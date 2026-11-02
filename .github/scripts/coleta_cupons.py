"""
Cupons do Mercado Livre para o site (cupons.json deste repositorio).

Fonte: pagina do Mercado Livre no Promobit (JSON __NEXT_DATA__, campo serverCoupons), a mesma
do cupons.py do canal de ofertas. Entram so cupons que podem servir para whey:
  - de Suplementos/Saude (o whey fica em Saude > Suplementos Alimentares no ML): "suplementos": true,
    e o site pode mostrar o selo "pode ter cupom" nos wheys que atingem o minimo;
  - gerais ("Selecionados" ou sem categoria): so na caixa de cupons, com o aviso de que valem
    para produtos selecionados.
Ficam de fora: outras categorias (Casa, Automotivo...), codigos exclusivos de outros sites,
vencidos e publicados ha muito tempo (os de "Selecionados" tem limite de usos e esgotam rapido).

Roda no GitHub Actions (.github/workflows/cupons.yml). Uso local sem gravar: --teste
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

FONTE = "https://www.promobit.com.br/promocoes/loja/mercado-livre/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
CAMINHO = "cupons.json"
IDADE_GERAL_DIAS = 2         # gerais publicados ha mais tempo costumam ter esgotado
IDADE_SUPLEMENTOS_DIAS = 7
MAX_CUPONS = 6
EXCLUSIVOS = re.compile(r"PROMOBIT|PELANDO|GATRY|CUPONOMIA|MEUCUPOM|PROMOBY|ZOOM|BUSCAPE|MELIUZ", re.I)
SUPLEMENTOS = re.compile(r"suplement|sa[uú]de|whey|prote[ií]n|nutri", re.I)
GERAL = re.compile(r"^(selecionad|todo o site|produtos selecionados)", re.I)
BRT = timezone(timedelta(hours=-3))


def valor(txt):
    return float(txt.replace(".", "").replace(",", "."))


def interpretar(c):
    titulo = c.get("couponTitle") or ""
    texto = f"{titulo} {c.get('couponInstructions') or ''}"
    curto = c.get("couponDiscountShort") or ""
    pct = re.search(r"(\d+)\s*%", curto or titulo)
    fixo = re.search(r"R\$\s*([\d.,]+)\s*(?:de desconto|OFF)", curto, re.I)
    minimo = re.search(r"(?:acima de|a partir de|m[ií]nima(?: de)?)\s*R\$\s*([\d.,]+)", texto, re.I)
    teto = re.search(r"(?:at[ée]|limitad[oa] a|m[áa]ximo(?: de)?)\s*R\$\s*([\d.,]+)", texto, re.I)
    em = re.search(r"\)\s*em\s+(.+)$|\bem\s+([A-ZÀ-Ú][^\d()]+)$", titulo)
    return {
        "codigo": (c.get("couponCode") or "").strip(),
        "titulo": titulo,
        "pct": int(pct.group(1)) if pct else None,
        "fixo": valor(fixo.group(1)) if fixo and not pct else None,
        "minimo": valor(minimo.group(1)) if minimo else 0.0,
        "teto": valor(teto.group(1)) if teto else None,
        "categoria": (em.group(1) or em.group(2)).strip() if em else "",
        "automatico": any("não é necessário" in t.lower() for t in c.get("couponTag") or []),
        "publicado": datetime.strptime(c["couponPublished"], "%Y-%m-%dT%H:%M:%S%z"),
        "ate": c.get("couponUntil"),
    }


def baixar_cupons():
    req = urllib.request.Request(FONTE, headers={"User-Agent": UA, "Accept-Language": "pt-BR",
                                                 "Referer": "https://www.promobit.com.br/"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Promobit sem __NEXT_DATA__ (o site mudou?)")
    return (json.loads(m.group(1))["props"]["pageProps"].get("serverCoupons") or {}).get("coupons") or []


def filtrar(brutos, agora):
    lista, vistos = [], set()
    for c in brutos:
        if c.get("storeSlug") != "mercado-livre" or c.get("couponStatusName") != "APPROVED":
            continue
        try:
            x = interpretar(c)
        except (KeyError, ValueError):
            continue
        if not x["codigo"] and not x["automatico"]:
            continue
        if x["codigo"] and EXCLUSIVOS.search(x["codigo"]):
            continue
        if not x["pct"] and not x["fixo"]:
            continue
        suple = bool(SUPLEMENTOS.search(x["categoria"] + " " + x["titulo"]))
        geral = not x["categoria"] or bool(GERAL.match(x["categoria"]))
        if not suple and not geral:
            continue
        idade = IDADE_SUPLEMENTOS_DIAS if suple else IDADE_GERAL_DIAS
        if agora - x["publicado"] > timedelta(days=idade):
            continue
        if x["ate"]:
            try:
                if datetime.fromisoformat(str(x["ate"]).replace("Z", "+00:00")) < agora:
                    continue
            except ValueError:
                pass
        chave = x["codigo"] or x["titulo"]
        if chave in vistos:
            continue
        vistos.add(chave)
        lista.append({
            "codigo": x["codigo"],
            "pct": x["pct"], "fixo": x["fixo"], "minimo": x["minimo"], "teto": x["teto"],
            "categoria": x["categoria"] or "Selecionados",
            "suplementos": suple,
            "automatico": x["automatico"],
            "ate": x["ate"],
            "publicado": x["publicado"].astimezone(BRT).isoformat(timespec="minutes"),
        })
    # suplementos primeiro, depois os de maior desconto
    lista.sort(key=lambda c: (c["suplementos"], c["pct"] or 0, c["fixo"] or 0), reverse=True)
    return lista[:MAX_CUPONS]


def gh(caminho, corpo=None, metodo=None):
    cab = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode()
        cab["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{caminho}",
                                 data=dados, headers=cab, method=metodo)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    agora = datetime.now(timezone.utc)
    lista = filtrar(baixar_cupons(), agora)
    saida = {"atualizado": agora.astimezone(BRT).isoformat(timespec="minutes"), "fonte": "Promobit", "cupons": lista}
    texto = json.dumps(saida, ensure_ascii=False, indent=2) + "\n"
    for c in lista:
        print(f"  {c['codigo'] or '(automatico)':16} {c['pct'] or ''}% R${c['fixo'] or ''} min R${c['minimo']:.0f} "
              f"teto {c['teto']} | {c['categoria']}{' [suplementos]' if c['suplementos'] else ''}")
    print(f"{len(lista)} cupons")
    if "--teste" in sys.argv:
        return 0

    try:
        atual = gh(f"contents/{CAMINHO}")
        sha = atual["sha"]
        antigo = json.loads(base64.b64decode(atual["content"]).decode("utf-8"))
        if antigo.get("cupons") == lista:
            print("sem mudanca nos cupons; nada gravado")
            return 0
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        sha = None
    corpo = {"message": f"Cupons do ML: {len(lista)}", "content": base64.b64encode(texto.encode("utf-8")).decode()}
    if sha:
        corpo["sha"] = sha
    r = gh(f"contents/{CAMINHO}", corpo, "PUT")
    print(f"gravado: commit {r['commit']['sha'][:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
