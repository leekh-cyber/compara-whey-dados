"""
Coleta diaria dos precos da Shopee pela API de afiliados e grava no precos.json deste
repositorio (campo "lojas", loja "Shopee").

Roda no GitHub Actions (.github/workflows/precos-shopee.yml), sem depender do PC.
  - Produtos: os links de afiliado da Shopee no js/afiliados.js do repo do site
    (leekh-cyber/comparawhey). O link curto so e aberto (sem seguir ate o produto) na primeira
    vez, para descobrir a loja e o anuncio; depois os codigos ficam na url de lojas[Shopee].
  - Preco: priceMin/priceMax do anuncio (productOfferV2). Se forem iguais, e o preco do anuncio.
    Se forem diferentes, e uma faixa: grava o menor com "aPartir": true, e a oferta nao muda.
  - Tamanho: se o nome do anuncio citar pesos e nenhum bater com a embalagem, o produto falha.
  - Para cada preco unico: a oferta do whey so muda se ele for menor que a atual
    (mesma regra do coleta_precos_pc.py). oferta.afiliado nunca e alterado.
  - Falhas nao gravam preco daquele produto e deixam a execucao vermelha no Actions.

Variaveis de ambiente: SHOPEE_APP_ID, SHOPEE_APP_SECRET, GITHUB_TOKEN, GITHUB_REPOSITORY.
Uso sem gravar: python coleta_shopee.py --teste
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from coleta_ml_api import AFILIADOS, CAMINHO, FAIXA, gh, ponto_do_dia

API = "https://open-api.affiliate.shopee.com.br/graphql"
LOJA = "Shopee"
PAUSA = 2
TOLERANCIA_G = 10    # mesma folga de tamanho do site (mesmoTamanho, em core.js)


def produtos():
    """{whey_id: (link curto, embalagem)} dos links da Shopee no afiliados.js."""
    js = urllib.request.urlopen(AFILIADOS, timeout=30).read().decode("utf-8")
    return {m.group(1): (m.group(2), int(m.group(3))) for m in
            re.finditer(r'"([\w-]+)":\s*\["(https://s\.shopee\.com\.br/[^"]+)",\s*(\d+)', js)}


class _SemSeguir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def ids_do_link(link):
    """(loja, anuncio) a partir do redirecionamento do link curto, sem abrir a pagina do produto."""
    op = urllib.request.build_opener(_SemSeguir)
    try:
        op.open(urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"}), timeout=30)
    except urllib.error.HTTPError as e:
        destino = e.headers.get("Location") or ""
        m = re.search(r"-i\.(\d+)\.(\d+)|shopee\.com\.br/[^/?]+/(\d+)/(\d+)", destino)
        if m:
            a, b, c, d = m.groups()
            return int(a or c), int(b or d)
    raise RuntimeError("link curto sem os codigos do anuncio")


def ids_da_url(url):
    m = re.search(r"/product/(\d+)/(\d+)", url or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def consultar(loja, item):
    q = ("{productOfferV2(shopId:%d,itemId:%d){nodes{itemId shopId productName shopName "
         "priceMin priceMax}}}") % (loja, item)
    corpo = json.dumps({"query": q}, separators=(",", ":"))
    app, seg = os.environ["SHOPEE_APP_ID"].strip(), os.environ["SHOPEE_APP_SECRET"].strip()
    ts = str(int(time.time()))
    ass = hashlib.sha256((app + ts + corpo + seg).encode()).hexdigest()
    req = urllib.request.Request(API, data=corpo.encode(), headers={
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={app}, Timestamp={ts}, Signature={ass}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode("utf-8"))
    if res.get("errors"):
        raise RuntimeError(res["errors"][0].get("message", "erro da API")[:150])
    nos = res["data"]["productOfferV2"]["nodes"]
    if not nos:
        raise RuntimeError("anuncio nao encontrado na API (fora do ar?)")
    return nos[0]


def ler(url_salva, link, embalagem):
    """Retorna (url, valor, aPartir, nome do anuncio)."""
    ids = ids_da_url(url_salva) or ids_do_link(link)
    n = consultar(*ids)
    pmin, pmax = float(n["priceMin"]), float(n["priceMax"])
    if not FAIXA[0] <= pmin <= FAIXA[1]:
        raise RuntimeError(f"preco fora da faixa: {pmin}")
    pesos = [float(a.replace(",", ".")) * (1000 if u.lower() == "kg" else 1)
             for a, u in re.findall(r"(\d+(?:[.,]\d+)?)\s*(kg|g)\b", n["productName"], re.I)]
    if pesos and all(abs(g - embalagem) > TOLERANCIA_G for g in pesos):
        raise RuntimeError(f"anuncio de outro tamanho ({embalagem} g esperados): {n['productName'][:80]}")
    return f"https://shopee.com.br/product/{ids[0]}/{ids[1]}", pmin, pmin != pmax, n["productName"]


def aplicar(dados, leituras, hoje):
    ofertas = 0
    for k, url, emb, valor, a_partir in leituras:
        w = dados["wheys"][k]
        lojas = w.setdefault("lojas", [])
        l = next((x for x in lojas if x.get("loja") == LOJA), None)
        if l is None:
            l = {"loja": LOJA, "url": url, "valor": valor, "data": hoje, "embalagem": emb, "historico": []}
            lojas.append(l)
        l.update(url=url, valor=valor, data=hoje, embalagem=emb)
        if a_partir:
            l["aPartir"] = True
        else:
            l.pop("aPartir", None)
        ponto_do_dia(l.setdefault("historico", []), hoje, valor)

        oferta = w.get("oferta")
        if not a_partir and (oferta is None or oferta.get("valor") is None or valor < oferta["valor"]):
            if oferta is None:
                oferta = w["oferta"] = {}
            oferta.update(valor=valor, loja=LOJA, data=hoje, url=url)
            ponto_do_dia(w.setdefault("historico", []), hoje, valor, loja=LOJA)
            print(f"  oferta {k}: -> R${valor:.2f}")
            ofertas += 1
    dados["atualizado"] = hoje
    return ofertas


def baixar():
    j = gh(f"contents/{CAMINHO}")
    return json.loads(base64.b64decode(j["content"]).decode("utf-8-sig")), j["sha"]


def main():
    teste = "--teste" in sys.argv
    hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).date().isoformat()  # data de Brasilia
    dados, _ = baixar()
    alvos = {k: v for k, v in produtos().items() if k in dados["wheys"]}
    print(f"===== coleta Shopee {hoje}: {len(alvos)} produtos =====")

    leituras, falhas = [], []
    for k, (link, emb) in alvos.items():
        salva = next((l.get("url") for l in dados["wheys"][k].get("lojas", []) if l.get("loja") == LOJA), None)
        for t in range(2):
            try:
                url, v, a_partir, nome = ler(salva, link, emb)
                print(f"  {k} R${v:.2f}{' (a partir de)' if a_partir else ''} | {nome[:70]}")
                leituras.append((k, url, emb, v, a_partir))
                break
            except Exception as e:
                erro = str(e)[:200]
                time.sleep(PAUSA * 5)
        else:
            print(f"  FALHOU {k}: {erro}")
            falhas.append((k, erro))
        time.sleep(PAUSA)

    if not leituras:
        print("nenhum preco lido; nada gravado")
        return 1
    if teste:
        print(f"teste: nada gravado ({len(leituras)} lidos, {len(falhas)} falhas)")
        return 1 if falhas else 0

    # baixa logo antes de gravar e tenta de novo se outra coleta mexeu no arquivo
    for _ in range(3):
        dados, sha = baixar()
        ofertas = aplicar(dados, leituras, hoje)
        texto = json.dumps(dados, ensure_ascii=False, indent=2) + "\n"
        try:
            r = gh(f"contents/{CAMINHO}", {
                "message": f"Preços da Shopee {hoje}: {len(leituras)} lidos, {len(falhas)} falhas",
                "sha": sha, "content": base64.b64encode(texto.encode("utf-8")).decode(),
            }, "PUT")
            print(f"gravado: commit {r['commit']['sha'][:7]}")
            break
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            print("arquivo mudou durante a coleta; tentando de novo")
            time.sleep(5)
    else:
        raise RuntimeError("nao consegui gravar (conflito repetido)")

    print(f"resumo: {len(leituras)} precos atualizados, {ofertas} ofertas baixaram, {len(falhas)} falhas")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
