"""
Gera posts de oferta no mesmo formato dos canais de achados do WhatsApp.

Uso:
  python gerar_post.py https://meli.la/XXXXXXX
  python gerar_post.py https://meli.la/XXXXXXX --cupom ACTINE15 --pct 15
  python gerar_post.py https://meli.la/XXXXXXX --titulo "Whey Growth 1kg Chocolate"

Como funciona (engenharia reversa):
  1. O link meli.la (gerado no Gerador de links da Central de Afiliados) redireciona
     para o seu perfil social: mercadolivre.com.br/social/<perfil>?matt_word=...&ref=...
  2. Essa pagina traz o produto em destaque no bloco "card-featured", com titulo,
     preco anterior (previous_price), preco atual/Pix (current_price), selo de desconto
     e selo de loja oficial ({icon_cockade}).
  3. O post usa: De = preco anterior (cortado), Por = preco Pix menos o cupom, se
     houver (arredondado), sempre em reais inteiros.

Envio opcional para o Canal via Whapi.Cloud (configure o whapi.json; veja whapi.py):
  python gerar_post.py https://meli.la/XXXXXXX --enviar
"""
import argparse
import html
import json
import math
import os
import re
import sys
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def baixar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.geturl(), r.read().decode("utf-8", errors="replace")


def extrair(link):
    url_final, pagina = baixar(link)
    i = pagina.find('"id":"card-featured"')
    if i < 0:
        raise SystemExit("Nao achei o produto em destaque. O link e um meli.la de afiliado?")
    # O primeiro item da lista "polycards" do bloco em destaque e o produto do link.
    # Ler so esse objeto evita misturar precos de outros produtos recomendados na pagina.
    j = pagina.find('"polycards":[', i)
    if j < 0:
        raise SystemExit("Pagina sem lista de produtos (formato do ML mudou?)")
    item = json.JSONDecoder().raw_decode(pagina[j + len('"polycards":'):])[0][0]
    comp = {c.get("type"): c.get(c.get("type"), {}) for c in item.get("components", [])}
    preco = comp.get("price", {})

    titulo = comp.get("title", {}).get("text")
    vendedor = comp.get("seller", {}).get("text", "")
    de = (preco.get("previous_price") or {}).get("value")
    por = (preco.get("current_price") or {}).get("value")
    selo = (preco.get("discount_label") or {}).get("text", "")
    m_img = re.search(r'<meta property="og:image" content="([^"]+)"', pagina)
    m_tag = re.search(r'matt_word=([^&]+)', url_final)

    return {
        "titulo": titulo,
        "de": float(de) if de else None,
        "por": float(por) if por else None,
        "selo": selo,
        "loja_oficial": "{icon_cockade}" in vendedor,
        "vendedor": vendedor.replace("{icon_cockade}", "").strip(),
        "imagem": html.unescape(m_img.group(1)) if m_img else None,
        "produto": item.get("metadata", {}).get("product_id") or item.get("metadata", {}).get("id"),
        "url_produto": ("https://" + item["metadata"]["url"].split("#")[0]) if item.get("metadata", {}).get("url") else None,
        "url_final": url_final,
        "etiqueta": m_tag.group(1) if m_tag else None,
    }


AVISO_AFILIADO = "_Canal de afiliados: posso ganhar comissão pelos links._"


def reais(v, arredondar=False):
    # Nos posts analisados: "De" e cortado (91,99 -> 91) e "Por" e arredondado (46,99 -> 47)
    return f"R${round(v) if arredondar else math.floor(v + 1e-9)}"


def montar_post(d, link, cupom=None, pct=None, titulo=None, cupom_detalhe=None, com_cupom=None):
    por = d["por"]
    if pct:
        por = por * (1 - pct / 100)
    no_pix = " no pix" if "pix" in d["selo"].lower() else ""

    # Espaco antes do * final faz o WhatsApp nao aplicar o negrito.
    linhas = [f"*{str(titulo or d['titulo']).strip()}*", ""]
    if d["de"] and d["de"] > por:
        linhas.append(f"De ~{reais(d['de'])}~")
    rotulo = "A partir de" if d.get("a_partir") else "Por"
    linhas.append(f"{rotulo} *{reais(por, arredondar=True)}* 🔥{no_pix}")
    if cupom:
        linhas += ["", f"🎟️ Cupom: *{cupom}*" + (f" ({cupom_detalhe})" if cupom_detalhe else "")]
        if com_cupom and com_cupom < por:
            linhas.append(f"💸 Com o cupom: *{reais(com_cupom, arredondar=True)}*")
    linhas += ["", f"👉 *Pegar promoção:* {link}"]
    if d["loja_oficial"]:
        linhas += ["", "Loja oficial no MELI!"]
    linhas += ["", AVISO_AFILIADO]
    return "\n".join(linhas)


def enviar_whapi(texto, imagem):
    from whapi import postar
    return postar(texto, imagem)


def main():
    ap = argparse.ArgumentParser(description="Gera post de oferta a partir de um link meli.la")
    ap.add_argument("link", help="link meli.la, ou URL do produto (gera o meli.la sozinho com --tag)")
    ap.add_argument("--tag", default=os.environ.get("ML_TAG"), help="sua etiqueta de afiliado (ou defina ML_TAG)")
    ap.add_argument("--cupom", help="codigo do cupom (ex: ACTINE15)")
    ap.add_argument("--pct", type=float, help="desconto do cupom em %% (ex: 15)")
    ap.add_argument("--titulo", help="titulo personalizado (padrao: titulo do anuncio)")
    ap.add_argument("--enviar", action="store_true", help="posta no Canal via Whapi.Cloud")
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    if "meli.la/" not in a.link:
        if not a.tag:
            raise SystemExit("Para URL de produto informe --tag (ou defina ML_TAG).")
        from ml_afiliado import gerar_link
        a.link = gerar_link(a.link, a.tag)
        print(f"Link gerado: {a.link}\n")

    d = extrair(a.link)
    texto = montar_post(d, a.link, a.cupom, a.pct, a.titulo)

    print(texto)
    print("\n---")
    print(f"Etiqueta de afiliado: {d['etiqueta']}  |  Vendedor: {d['vendedor']}  |  Selo: {d['selo']}")
    print(f"Imagem: {d['imagem']}")

    if a.enviar:
        print(enviar_whapi(texto, d["imagem"]))


if __name__ == "__main__":
    main()
