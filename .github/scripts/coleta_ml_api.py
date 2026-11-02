"""
Coleta diaria dos precos do Mercado Livre pela API oficial (aplicativo "pesquisawheysite")
e grava no precos.json deste repositorio (campo "lojas", loja "Mercado Livre").

Roda no GitHub Actions (.github/workflows/precos-ml.yml), sem depender do PC.
  - Produtos: os anuncios de afiliado do site/js/afiliados.js do repo do site
    (leekh-cyber/comparawhey). So os de catalogo (/p/MLB...); os anuncios avulsos
    (/up/MLBU...) a API nao abre, e esses continuam na coleta do PC.
  - Preco: o primeiro anuncio de products/{id}/items (o que o ML mostra na pagina do
    produto). E o preco do anuncio, sem o desconto do Pix.
  - Para cada preco: lojas[ML] ganha valor/data/historico; a oferta do whey so muda se
    o preco for menor que a atual (mesma regra do coleta_precos_pc.py).

Variaveis de ambiente: ML_CLIENT_ID, ML_CLIENT_SECRET, GITHUB_TOKEN, GITHUB_REPOSITORY.
Uso local sem gravar: python coleta_ml_api.py --teste
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AFILIADOS = "https://raw.githubusercontent.com/leekh-cyber/comparawhey/main/js/afiliados.js"
API = "https://api.mercadolibre.com"
CAMINHO = "precos.json"
LOJA_ML = "Mercado Livre"
FAIXA = (30, 1500)   # precos fora disso sao tratados como leitura errada
PAUSA = 1


def pedir(url, dados=None, cab=None, metodo=None):
    corpo = None
    if dados is not None:
        corpo = dados if isinstance(dados, bytes) else urllib.parse.urlencode(dados).encode()
    req = urllib.request.Request(url, data=corpo, headers=cab or {}, method=metodo)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def token():
    t = pedir(f"{API}/oauth/token", {
        "grant_type": "client_credentials",
        "client_id": os.environ["ML_CLIENT_ID"],
        "client_secret": os.environ["ML_CLIENT_SECRET"],
    }, {"Accept": "application/json"})
    return t["access_token"]


def produtos():
    """{whey_id: (url do produto, embalagem)} dos anuncios de catalogo do afiliados.js."""
    js = urllib.request.urlopen(AFILIADOS, timeout=30).read().decode("utf-8")
    itens = {}
    for m in re.finditer(r'"([\w-]+)":\s*\[\{\s*loja:\s*"Mercado Livre",\s*link:\s*"[^"]+",\s*'
                         r'embalagem:\s*(\d+),\s*url:\s*"([^"]+)"', js):
        itens[m.group(1)] = (m.group(3), int(m.group(2)))
    for m in re.finditer(r'"([\w-]+)":\s*\["https://meli\.la/[^"]+",\s*"([^"]+)",\s*(\d+),', js):
        itens[m.group(1)] = (m.group(2), int(m.group(3)))
    return {k: v for k, v in itens.items() if re.search(r"/p/MLB\d+", v[0])}


def ler(tok, url):
    pid = re.search(r"/p/(MLB\d+)", url).group(1)
    r = pedir(f"{API}/products/{pid}/items?limit=1", cab={"Authorization": f"Bearer {tok}"})
    if not r.get("results"):
        raise RuntimeError("sem anuncio ativo")
    v = float(r["results"][0]["price"])
    if not FAIXA[0] <= v <= FAIXA[1]:
        raise RuntimeError(f"preco fora da faixa: {v}")
    return v


# ---------- precos.json (mesma logica do coleta_precos_pc.py) ----------

def ponto_do_dia(hist, hoje, valor, **extra):
    if hist and hist[-1].get("data") == hoje:
        hist[-1].update(valor=valor, **extra)
    else:
        hist.append({"data": hoje, "valor": valor, **extra})


def aplicar(dados, leituras, hoje):
    ofertas = 0
    for k, url, emb, valor in leituras:
        w = dados["wheys"].get(k)
        if w is None:
            print(f"  aviso: {k} nao existe no precos.json (ignorado)")
            continue
        lojas = w.setdefault("lojas", [])
        l = next((x for x in lojas if x.get("loja") == LOJA_ML), None)
        if l is None:
            l = {"loja": LOJA_ML, "url": url, "valor": valor, "data": hoje, "embalagem": emb, "historico": []}
            lojas.append(l)
        l["valor"] = valor
        l["data"] = hoje
        if emb:
            l["embalagem"] = emb
        ponto_do_dia(l.setdefault("historico", []), hoje, valor)

        oferta = w.get("oferta")
        if oferta is None or oferta.get("valor") is None or valor < oferta["valor"]:
            if oferta is None:
                oferta = w["oferta"] = {}
            oferta.update(valor=valor, loja=LOJA_ML, data=hoje, url=l.get("url") or url)
            ponto_do_dia(w.setdefault("historico", []), hoje, valor, loja=LOJA_ML)
            print(f"  oferta {k}: -> R${valor:.2f}")
            ofertas += 1
    dados["atualizado"] = hoje
    return ofertas


def gh(caminho, corpo=None, metodo=None):
    cab = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    if corpo is not None:
        corpo = json.dumps(corpo).encode()
        cab["Content-Type"] = "application/json"
    return pedir(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{caminho}", corpo, cab, metodo)


def main():
    teste = "--teste" in sys.argv
    hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).date().isoformat()  # data de Brasilia
    tok = token()
    alvos = produtos()
    print(f"===== coleta ML pela API {hoje}: {len(alvos)} produtos =====")

    leituras, falhas = [], []
    for k, (url, emb) in alvos.items():
        for t in range(2):
            try:
                v = ler(tok, url)
                print(f"  {k} R${v:.2f}")
                leituras.append((k, url, emb, v))
                break
            except Exception as e:
                erro = str(e)[:200]
                time.sleep(PAUSA * 3)
        else:
            print(f"  FALHOU {k}: {erro}")
            falhas.append((k, erro))
        time.sleep(PAUSA)

    if not leituras:
        print("nenhum preco lido; nada gravado")
        return 1
    if teste:
        print("teste: nada gravado")
        return 0

    # baixa logo antes de gravar e tenta de novo se a rotina da nuvem ou o PC mexeram no arquivo
    for _ in range(3):
        j = gh(f"contents/{CAMINHO}")
        dados = json.loads(base64.b64decode(j["content"]).decode("utf-8-sig"))
        ofertas = aplicar(dados, leituras, hoje)
        texto = json.dumps(dados, ensure_ascii=False, indent=2) + "\n"
        try:
            r = gh(f"contents/{CAMINHO}", {
                "message": f"Preços do ML pela API {hoje}: {len(leituras)} lidos, {len(falhas)} falhas",
                "sha": j["sha"], "content": base64.b64encode(texto.encode("utf-8")).decode(),
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
    return 1 if falhas and len(falhas) > len(leituras) else 0


if __name__ == "__main__":
    sys.exit(main())
