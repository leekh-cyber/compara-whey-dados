"""
Shopee pela API oficial de afiliados: preco atual, desconto, foto e o SEU link curto.

Credenciais em shopee.json (App ID e App Secret do painel de afiliados). Nao compartilhe esse arquivo.
A pagina do produto bloqueia leitura automatica, entao tudo vem da API.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_CONFIG = os.path.join(PASTA, "shopee.json")
API = "https://open-api.affiliate.shopee.com.br/graphql"


def config():
    try:
        with open(ARQ_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    # Na nuvem (GitHub Actions) as credenciais vem dos segredos do repositorio.
    return ((os.environ.get("SHOPEE_APP_ID") or cfg.get("app_id", "")).strip(),
            (os.environ.get("SHOPEE_APP_SECRET") or cfg.get("app_secret", "")).strip())


def disponivel():
    return all(config())


def chamar(consulta):
    app, segredo = config()
    corpo = json.dumps({"query": consulta}, separators=(",", ":"))
    ts = str(int(time.time()))
    assinatura = hashlib.sha256((app + ts + corpo + segredo).encode()).hexdigest()
    req = urllib.request.Request(API, data=corpo.encode(), headers={
        "Content-Type": "application/json", "User-Agent": "ComparaWhey/1.0",
        "Authorization": f"SHA256 Credential={app}, Timestamp={ts}, Signature={assinatura}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode("utf-8"))
    if res.get("errors"):
        raise RuntimeError("API da Shopee: " + str(res["errors"][0].get("message"))[:150])
    return res["data"]


class _SemSeguir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def destino(link):
    """Para onde o link curto aponta, sem abrir a pagina do produto."""
    abridor = urllib.request.build_opener(_SemSeguir)
    try:
        abridor.open(urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"}), timeout=30)
    except urllib.error.HTTPError as e:
        return e.headers.get("Location") or ""
    return ""


def ids_do_link(link):
    """(loja, anuncio) a partir do link curto."""
    m = re.search(r"-i\.(\d+)\.(\d+)|shopee\.com\.br/[^/?]+/(\d+)/(\d+)", destino(link))
    if not m:
        raise RuntimeError("link curto sem os codigos do anuncio")
    a, b = [x for x in m.groups() if x]
    return int(a), int(b)


def produto(loja, item):
    """Mesmos campos de gerar_post.extrair, para montar_post e os filtros de desconto."""
    nos = chamar("{productOfferV2(shopId:%d,itemId:%d){nodes{productName priceMin priceMax priceDiscountRate imageUrl}}}"
                 % (loja, item))["productOfferV2"]["nodes"]
    if not nos:
        raise RuntimeError("anuncio nao encontrado na API")
    n = nos[0]
    por, teto = float(n["priceMin"]), float(n["priceMax"])
    taxa = float(n.get("priceDiscountRate") or 0)
    return {
        "titulo": n["productName"],
        "de": por / (1 - taxa / 100) if 0 < taxa < 100 else None,
        "por": por,
        "a_partir": por != teto,  # anuncio com variacoes de preco
        "selo": "",
        "loja_oficial": False,
        "imagem": n.get("imageUrl"),
        "produto": f"shopee:{item}",
    }


def gerar_link(loja, item):
    url = f"https://shopee.com.br/product/{loja}/{item}"
    return chamar('mutation{generateShortLink(input:{originUrl:"%s",subIds:["canal"]}){shortLink}}' % url)["generateShortLink"]["shortLink"]
