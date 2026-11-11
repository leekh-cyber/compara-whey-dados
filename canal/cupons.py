"""
Cupons do Mercado Livre (fonte: pagina do ML no Promobit, campo serverCoupons).

Usado pelo descobrir_ofertas.py de dois jeitos:
  1. Nos achados: se houver cupom da mesma categoria e a compra atingir o minimo,
     o post ganha a linha "🎟️ Cupom: *CODIGO* (15% OFF acima de R$79)".
  2. Posts proprios de cupom (no maximo CUPONS_POR_DIA), com link para o seu
     perfil social de afiliado.
"""
import re
from datetime import datetime, timedelta, timezone

from gerar_post import AVISO_AFILIADO

IDADE_MAXIMA_DIAS = 2          # cupons publicados ha mais tempo sao ignorados
CUPONS_POR_DIA = 5             # posts proprios de cupom por dia
CUPONS_POR_RODADA = 1          # espalha os cupons do dia pelas rodadas
INTERVALO_CUPOM_HORAS = 3      # tempo minimo entre posts de cupom
DESCONTO_MINIMO_POST = 15      # posts de cupom so com >= 15% ou >= R$30 de desconto
VALOR_MINIMO_POST = 30
# Codigos exclusivos de outros sites/afiliados (podem nao funcionar fora do link deles)
EXCLUSIVOS = re.compile(r"PROMOBIT|PELANDO|GATRY|CUPONOMIA|MEUCUPOM|PROMOBY|ZOOM|BUSCAPE|MELIUZ", re.I)

# categoria do Promobit -> palavras que aparecem no titulo do cupom ("... em Ferramentas e Construcao")
CATEGORIAS = {
    "ferramentas-e-jardim": ["ferrament", "constru", "jardim"],
    "casa-e-construcao": ["constru", "ferrament", "casa"],
    "eletronicos-audio-e-video": ["eletrônic", "eletronic", "tecnolog", "áudio"],
    "informatica": ["informátic", "informatic", "eletrônic", "tecnolog"],
    "smartphones-tablets-e-telefones": ["celular", "smartphone", "eletrônic", "tecnolog"],
    "games": ["game", "eletrônic", "tecnolog"],
    "pecas-e-acessorios-para-automoveis": ["automotiv", "auto"],
    "moveis-e-decoracao": ["móve", "move", "decora", "casa"],
    "cama-mesa-e-banho": ["casa", "decora"],
    "utensilios-domesticos": ["casa", "cozinha"],
    "eletrodomesticos": ["eletrodom", "eletro "],
    "eletroportateis": ["eletroport", "eletro "],
    "bebes-e-criancas": ["bebê", "bebe", "infantil", "criança"],
    "brinquedos-e-hobbies": ["brinquedo", "infantil"],
    "moda-e-calcados-femininos": ["moda", "calçado", "roupa"],
    "moda-e-calcados-masculinos": ["moda", "calçado", "roupa"],
    "relogios-e-joias": ["relógio", "joia", "acessório"],
    "esporte-e-lazer": ["esporte", "sports", "fitness"],
    "suplementos-e-fitness": ["suplement", "fitness", "esporte", "sports"],
    "perfumes-e-beleza": ["beleza", "perfum", "cosmét"],
    "saude-e-higiene": ["saúde", "higiene", "beleza", "farm"],
    "supermercado-e-delivery": ["mercado", "supermercado"],
    "petshop": ["pet"],
}


def _valor(txt):
    return float(txt.replace(".", "").replace(",", "."))


def interpretar(c):
    """Extrai desconto, minimo, teto e categoria do titulo/instrucoes do cupom."""
    titulo = c.get("couponTitle") or ""
    texto = f"{titulo} {c.get('couponInstructions') or ''}"
    pct = re.search(r"(\d+)\s*%", c.get("couponDiscountShort") or titulo)
    fixo = re.search(r"R\$\s*([\d.,]+)\s*(?:de desconto|OFF)", c.get("couponDiscountShort") or "", re.I)
    minimo = re.search(r"(?:acima de|a partir de|m[ií]nima(?: de)?)\s*R\$\s*([\d.,]+)", texto, re.I)
    teto = re.search(r"(?:at[ée]|limitad[oa] a|m[áa]ximo(?: de)?)\s*R\$\s*([\d.,]+)", texto, re.I)
    em = re.search(r"\)\s*em\s+(.+)$|\bem\s+([A-ZÀ-Ú][^\d()]+)$", titulo)
    categoria = (em.group(1) or em.group(2)).strip() if em else ""
    return {
        "id": str(c["couponId"]),
        "codigo": (c.get("couponCode") or "").strip(),
        "titulo": titulo,
        "pct": int(pct.group(1)) if pct else None,
        "fixo": _valor(fixo.group(1)) if fixo and not pct else None,
        "minimo": _valor(minimo.group(1)) if minimo else 0.0,
        "teto": _valor(teto.group(1)) if teto else None,
        "categoria": categoria,
        "automatico": any("não é necessário" in t.lower() for t in c.get("couponTag") or []),
        "publicado": datetime.strptime(c["couponPublished"], "%Y-%m-%dT%H:%M:%S%z"),
        "ate": c.get("couponUntil"),
    }


def resumo(c):
    """Ex.: '15% OFF acima de R$79, até R$60' ou 'R$40 OFF acima de R$199'."""
    desc = f"{c['pct']}% OFF" if c["pct"] else (f"R${c['fixo']:.0f} OFF" if c["fixo"] else "desconto")
    partes = [desc]
    if c["minimo"] > 1:
        partes.append(f"acima de R${c['minimo']:.0f}")
    txt = " ".join(partes)
    if c["pct"] and c["teto"]:
        txt += f", até R${c['teto']:.0f}"
    return txt


def cupons_validos(page_props):
    """Cupons recentes do ML, com codigo (ou automaticos), sem codigos exclusivos de terceiros."""
    brutos = (page_props.get("serverCoupons") or {}).get("coupons") or []
    agora = datetime.now(timezone.utc)
    lista = []
    for c in brutos:
        try:
            x = interpretar(c)
        except (KeyError, ValueError):
            continue
        if c.get("storeSlug") != "mercado-livre" or c.get("couponStatusName") != "APPROVED":
            continue
        if not x["codigo"] and not x["automatico"]:
            continue
        if x["codigo"] and EXCLUSIVOS.search(x["codigo"]):
            continue
        if agora - x["publicado"] > timedelta(days=IDADE_MAXIMA_DIAS):
            continue
        if x["ate"]:
            try:
                if datetime.fromisoformat(str(x["ate"]).replace("Z", "+00:00")) < agora:
                    continue
            except ValueError:
                pass
        if not x["pct"] and not x["fixo"]:
            continue
        lista.append(x)
    return lista


CODIGO = re.compile(r"[A-Z0-9]{4,24}")  # "Nubank", "Pix" e frases nao sao codigo de cupom


def codigo_valido(txt):
    cod = (txt or "").strip()
    return cod if CODIGO.fullmatch(cod) and not EXCLUSIVOS.search(cod) else None


def por_codigo(*page_props):
    """Codigo -> cupom (interpretado), dos cupons no ar nas paginas do Promobit, sem filtro de idade."""
    agora = datetime.now(timezone.utc)
    achados = {}
    for pp in page_props:
        for c in (pp.get("serverCoupons") or {}).get("coupons") or []:
            try:
                x = interpretar(c)
            except (KeyError, ValueError):
                continue
            if c.get("couponStatusName") != "APPROVED" or not x["codigo"] or not (x["pct"] or x["fixo"]):
                continue
            try:
                if x["ate"] and datetime.fromisoformat(re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", str(x["ate"]))) < agora:
                    continue
            except ValueError:
                pass
            achados.setdefault(x["codigo"].upper(), x)
    return achados


def com_cupom(c, preco):
    """Preco depois do cupom, ou None se a compra nao atinge o minimo."""
    if not c or not preco or preco < c["minimo"]:
        return None
    desconto = preco * c["pct"] / 100 if c["pct"] else c["fixo"]
    if c["teto"]:
        desconto = min(desconto, c["teto"])
    return max(preco - desconto, 0)


def registro(codigo, c):
    """O que o registro do post guarda do cupom, para marcar quando ele vencer ou esgotar."""
    return {"codigo": codigo, "id": c and c["id"], "publicado": c and c["publicado"].isoformat(), "ate": c and c["ate"]}


def riscar(texto, codigo):
    """Texto do post com a linha do cupom riscada e sem o preco com cupom."""
    linhas = []
    for linha in texto.split("\n"):
        if linha.startswith("🎟️ Cupom:"):
            linha = f"🎟️ ~Cupom: {codigo}~ _(venceu ou esgotou)_"
        elif linha.startswith("💸 Com o cupom:"):
            continue
        linhas.append(linha)
    return "\n".join(linhas)


def cupom_para(oferta, preco, cupons):
    """Melhor cupom de categoria compativel com a oferta (so cupons com codigo e minimo atingido)."""
    palavras = CATEGORIAS.get(oferta["categoria"], [])
    melhor, economia_melhor = None, 0
    for c in cupons:
        if not c["codigo"] or not c["categoria"] or preco < c["minimo"]:
            continue
        cat = c["categoria"].lower()
        if not any(p in cat for p in palavras):
            continue
        economia = preco * c["pct"] / 100 if c["pct"] else c["fixo"]
        if c["teto"]:
            economia = min(economia, c["teto"])
        if economia > economia_melhor:
            melhor, economia_melhor = c, economia
    return melhor


def post_cupom(c, link):
    linhas = ["🎟️ *CUPOM MERCADO LIVRE*", "", f"*{resumo(c)}*"]
    if c["categoria"]:
        linhas.append(f"em {c['categoria']}")
    linhas.append("")
    if c["codigo"]:
        linhas.append(f"Código: *{c['codigo']}*")
        linhas.append("_Aplique no carrinho antes de pagar_")
    else:
        linhas.append("_Desconto aplicado automaticamente nos itens participantes_")
    linhas += ["", "⚠️ Válido enquanto durar o limite de usos", "", f"👉 *Ver achados:* {link}", "", AVISO_AFILIADO]
    return "\n".join(linhas)


def vale_post(c):
    return (c["pct"] or 0) >= DESCONTO_MINIMO_POST or (c["fixo"] or 0) >= VALOR_MINIMO_POST
