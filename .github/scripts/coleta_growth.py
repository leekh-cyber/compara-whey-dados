"""
Coleta diaria dos precos da loja oficial da Growth (gsuplementos.com.br) e grava no
precos.json deste repositorio (campo "lojas", loja "Growth (oficial)").

Roda no GitHub Actions (.github/workflows/precos-growth.yml), sem depender do PC.
  - Produtos: as URLs da loja "Growth (oficial)" que ja estao no precos.json.
  - Preco: o HTML da pagina ja traz o "Valor do produto a vista" da aba Pix, sem navegador.
  - Tamanho: esse valor e o da variacao padrao. So vale se a pagina (ou, sem lista de pesos, o
    nome do produto) mostrar o peso da embalagem do precos.json; com varios pesos, usa o preco
    ao lado do peso certo. Senao, falha.
  - Para cada preco: lojas[Growth] ganha valor/data/historico; a oferta do whey so muda se
    o preco for menor que a atual (mesma regra do coleta_precos_pc.py).
  - Se a pagina pedir verificacao ou mudar de formato, o produto e pulado e a execucao
    termina com erro (fica vermelha no Actions), sem gravar preco duvidoso.

Variaveis de ambiente: GITHUB_TOKEN, GITHUB_REPOSITORY.
Uso local sem gravar: python coleta_growth.py --teste
"""
import base64
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from coleta_ml_api import CAMINHO, FAIXA, gh, ponto_do_dia

LOJA = "Growth (oficial)"
PAUSA = 3
DOSE_MAX = 100       # variacoes menores que isso sao doses avulsas (ex.: 30 g), nao potes
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def reais(txt):
    return float(txt.replace(".", "").replace(",", "."))


def gramas(numero, unidade):
    return float(numero.replace(",", ".")) * (1000 if unidade.lower() == "kg" else 1)


def pesos_e_precos(pagina):
    """Bloco de variacoes da pagina: ({gramas}, {gramas: preco, quando a pagina mostra}).
    Ex.: "Selecione: 450 g ... R$142,90 ... 1 kg ... R$259,90" ou "Peso: 1 kg ... 33 doses"."""
    texto = re.sub(r"<(script|style)\b.*?</\1>", "", pagina, flags=re.S | re.I)
    texto = html.unescape(re.sub(r"<[^>]+>", "\n", texto))
    m = re.search(r"\n\s*(?:Selecione|Peso):?\s*\n(.*?)\n\s*Comprar\s*\n", texto, re.S | re.I)
    if not m:
        return set(), {}
    bloco = re.sub(r"R\$\s*[\d.,]+\s*/\s*dose", "", m.group(1))
    pesos, precos, atual = set(), {}, None
    for linha in bloco.splitlines():
        p = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(kg|g)\s*", linha, re.I)
        if p:
            atual = gramas(p.group(1), p.group(2))
            if atual >= DOSE_MAX:
                pesos.add(atual)
            continue
        p = re.fullmatch(r"\s*R\$\s*([\d.]+,\d{2})\s*", linha)
        if p and atual is not None and atual >= DOSE_MAX:
            precos[atual] = reais(p.group(1))
    return pesos, precos


def peso_no_nome(pagina):
    """Peso no nome do produto (JSON-LD), para paginas sem bloco de pesos: "... 1Kg Sabor Natural"."""
    m = re.search(r'"@type":"Product".*?"name":"([^"]+)"', pagina, re.S)
    p = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g)\b", m.group(1), re.I) if m else None
    return gramas(p.group(1), p.group(2)) if p else None


def ler(url, embalagem):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(req, timeout=30) as r:
        pagina = r.read().decode("utf-8", "replace")

    m = re.search(r'"availability":"[^"]*/(\w+)"', pagina)
    if m and m.group(1) == "OutOfStock":
        raise RuntimeError("esgotado")
    m = re.search(r'pix-titulo-valor[^>]*>\s*R\$\s*([\d.]+,\d{2})', pagina)
    if not m:
        raise RuntimeError("sem preco Pix no HTML (verificacao ou pagina mudou?)")
    if not embalagem:
        raise RuntimeError("sem embalagem no precos.json")

    pesos, precos = pesos_e_precos(pagina)
    certo = lambda d: next((v for g, v in d.items() if abs(g - embalagem) < 1), None)
    v = certo(precos)
    if v is None:
        achados = pesos or {peso_no_nome(pagina)}
        if any(g is None or abs(g - embalagem) >= 1 for g in achados):
            raise RuntimeError(f"nao confirmei o preco de {embalagem} g (pesos na pagina: {achados})")
        v = reais(m.group(1))  # a pagina so tem o peso da embalagem
    if not FAIXA[0] <= v <= FAIXA[1]:
        raise RuntimeError(f"preco fora da faixa: {v}")
    return v


def aplicar(dados, leituras, hoje):
    ofertas = 0
    for k, valor in leituras:
        w = dados["wheys"][k]
        l = next(x for x in w["lojas"] if x.get("loja") == LOJA)
        l["valor"] = valor
        l["data"] = hoje
        ponto_do_dia(l.setdefault("historico", []), hoje, valor)

        oferta = w.get("oferta")
        if oferta is None or oferta.get("valor") is None or valor < oferta["valor"]:
            if oferta is None:
                oferta = w["oferta"] = {}
            oferta.update(valor=valor, loja=LOJA, data=hoje, url=l["url"])
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
    alvos = {k: l for k, w in dados["wheys"].items() for l in w.get("lojas", [])
             if l.get("loja") == LOJA and l.get("url")}
    print(f"===== coleta Growth {hoje}: {len(alvos)} produtos =====")

    leituras, falhas = [], []
    for k, l in alvos.items():
        for t in range(2):
            try:
                v = ler(l["url"], l.get("embalagem"))
                print(f"  {k} R${v:.2f}")
                leituras.append((k, v))
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
        print(f"teste: nada gravado ({len(falhas)} falhas)")
        return 1 if falhas else 0

    # baixa logo antes de gravar e tenta de novo se outra coleta mexeu no arquivo
    for _ in range(3):
        dados, sha = baixar()
        ofertas = aplicar(dados, leituras, hoje)
        texto = json.dumps(dados, ensure_ascii=False, indent=2) + "\n"
        try:
            r = gh(f"contents/{CAMINHO}", {
                "message": f"Preços da Growth {hoje}: {len(leituras)} lidos, {len(falhas)} falhas",
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
