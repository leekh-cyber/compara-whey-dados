"""
Descobre ofertas novas do Mercado Livre (qualquer categoria) e gera os posts com o SEU link.

Fonte: pagina de ofertas do Mercado Livre no Promobit (comunidade de promocoes), que lista
as ofertas mais recentes com preco, preco anterior e engajamento.

Caminho de cada oferta (engenharia reversa):
  Promobit /promocoes/loja/mercado-livre/  (JSON __NEXT_DATA__ com as ofertas)
    -> /Redirect/to/<id>/                    (pagina que contem o meli.la do Promobit)
    -> meli.la -> pagina social do ML        (produto em destaque: URL, preco atual)
    -> ml_afiliado gera o SEU meli.la a partir da URL do produto
    -> gerar_post monta o texto no formato do canal

O preco e conferido no proprio ML na hora: se a oferta ja acabou, nao posta.

Uso:
  python descobrir_ofertas.py              # mostra os posts e coloca em fila_posts.txt
  python descobrir_ofertas.py --enviar     # tambem posta no Canal (Whapi)
  python descobrir_ofertas.py --listar     # so lista as ofertas encontradas, sem gerar links

Com o PC desligado (GitHub Actions do repositorio compara-whey-dados, pasta canal/):
  python descobrir_ofertas.py --abastecer  # PC: gera os links do ML e deixa os posts prontos na fila da nuvem
  python descobrir_ofertas.py --nuvem --enviar   # nuvem: cupom, Shopee e os posts do ML que estao na fila
O link meli.la so sai do Chrome logado deste PC; o resto roda na nuvem, que e quem posta.
"""
import argparse
import base64
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import cupons
import shopee
from gerar_post import enviar_whapi, extrair, montar_post

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_VISTAS = os.path.join(PASTA, "ofertas_vistas.json")
ARQ_FILA = os.path.join(PASTA, "fila_posts.txt")
FONTE = "https://www.promobit.com.br/promocoes/loja/mercado-livre/"
FONTE_SHOPEE = "https://www.promobit.com.br/promocoes/loja/shopee/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# ---- Filtros (ajuste a vontade) ----
DESCONTO_MINIMO = 0.25      # so ofertas com >= 25% de desconto
PRECO_MINIMO = 15.0         # ignora itens muito baratos
IDADE_MAXIMA_HORAS = 6      # so ofertas publicadas nas ultimas N horas
MAX_POSTS_POR_RODADA = 3    # quantos posts no maximo por execucao
TOLERANCIA_PRECO = 0.05     # aceita o preco atual no ML ate 5% acima do anunciado
DESCONTO_SUSPEITO = 0.60    # acima disso o "De" costuma ser inflado: o post sai sem o "De"
DESCONTO_RESERVA = 0.15     # sem oferta nova: aceita desconto menor, conferido no preco atual do ML
DIAS_REPOST = 3             # sem oferta nova: reposta achados publicados ha mais de N dias
DOBRAR_DESDE = "2026-11-08"  # a partir desta data, com --enviar: 2 achados por rodada e ate 10 cupons por dia
CATEGORIAS_BLOQUEADAS = {"aplicativos-e-software"}  # ex.: {"bebidas"}
PAUSA_ENTRE_POSTS = 60      # segundos entre posts no Canal (--enviar)
# Link dos posts de cupom: o seu perfil social de afiliado (link da pagina inicial do ML nao paga comissao)
LINK_CUPONS = "https://www.mercadolivre.com.br/social/ferreiradasilvaalex61"

# ---- Nuvem ----
REPO = "leekh-cyber/compara-whey-dados"
ARQ_FILA_ML = os.path.join(PASTA, "fila_ml.json")  # na nuvem: lida do repositorio
REPO_VISTAS = "canal/ofertas_vistas.json"          # so a nuvem grava (ela e quem posta)
REPO_FILA_ML = "canal/fila_ml.json"                # so o PC grava
FILA_ALVO = 12              # posts do ML prontos na fila
FILA_HORAS = 12             # post da fila mais velho que isso nao sai (o preco pode ter mudado)
ACHADOS_POR_DIA = 50        # teto de achados da nuvem (os cupons tem o proprio)
INTERVALO_MINIMO_NUVEM = 20  # minutos: dois disparos proximos (cron-job.org e a agenda do GitHub) viram uma rodada
HORARIO_NUVEM = (8, 23)     # a nuvem so posta das 8h as 22h59

# ---- Datas comemorativas e grandes promocoes (datas.json) ----
ARQ_DATAS = os.path.join(PASTA, "datas.json")
ACHADOS_PICO = 80           # teto de achados nos dias de pico ("pico": true)
ACHADOS_POR_RODADA_PICO = 3  # 29 rodadas x 3 alcancam o teto de pico
HORA_AVISOS = 9             # contagem, vespera e aviso do dia saem a partir desta hora (1 por dia por campanha)
SELO = ""                   # selo da campanha no topo dos achados; so a rodada da nuvem liga


def baixar(url, tentativas=1):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR",
                                               "Referer": "https://www.promobit.com.br/"})
    for tentativa in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except OSError:
            if tentativa == tentativas - 1:
                raise
            time.sleep(10)


def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Promobit sem __NEXT_DATA__ (o site mudou?)")
    return json.loads(m.group(1))["props"]["pageProps"]


def ofertas_promobit(pp, loja="mercado-livre"):
    offs = pp["serverOffers"]["offers"]
    agora = datetime.now(timezone.utc)
    lista = []
    for o in offs:
        if o.get("offerPriceType") != "NORMAL" or o.get("storeParam") != loja:
            continue
        preco, antes = float(o.get("offerPrice") or 0), float(o.get("offerOldPrice") or 0)
        publicado = datetime.strptime(o["offerPublished"], "%Y-%m-%dT%H:%M:%S%z")
        lista.append({
            "id": str(o["offerId"]),
            "titulo": o["offerTitle"],
            "preco": preco,
            "antes": antes,
            "desconto": (1 - preco / antes) if antes > preco > 0 else 0,
            "categoria": o.get("categorySlug") or "",
            "engajamento": o.get("offerEngagementScore") or 0,
            "horas": (agora - publicado).total_seconds() / 3600,
        })
    return lista


def link_do_promobit(id_oferta):
    """A pagina /Redirect/to/<id>/ contem o meli.la (de afiliado do Promobit) do produto."""
    m = re.search(r'https://meli\.la/[A-Za-z0-9]+', baixar(f"https://www.promobit.com.br/Redirect/to/{id_oferta}/"))
    return m.group(0) if m else None


def internet_ok():
    try:
        socket.create_connection(("www.promobit.com.br", 443), timeout=5).close()
        return True
    except OSError:
        return False


def limite_de_pedidos(e):
    """HTTP 429: o site pediu para esperar. A rodada para em vez de insistir nas outras ofertas
    (insistir mantem o bloqueio); a proxima rodada tenta de novo. Diz qual site recusou."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 429:
        site = urllib.parse.urlsplit(e.filename or "").hostname or "site desconhecido"
        print(f"   {site} pediu para esperar (429): rodada encerrada, a proxima tenta de novo")
        return True
    return False


def preparar_ao_acordar(segundos=120):
    """Quando a tarefa acorda o PC: impede que ele volte a dormir durante a rodada e espera o Wi-Fi voltar."""
    if os.name != "nt":
        return
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    fim = time.time() + segundos
    while time.time() < fim:
        if internet_ok():
            return
        time.sleep(5)


def carregar_vistas():
    try:
        with open(ARQ_VISTAS, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def salvar_vistas(vistas):
    limite = (datetime.now() - timedelta(days=30)).isoformat()
    vistas = {k: v for k, v in vistas.items() if v.get("quando", "") >= limite}
    with open(ARQ_VISTAS, "w", encoding="utf-8") as f:
        json.dump(vistas, f, ensure_ascii=False, indent=1)


def publicar(texto, imagem, rotulo, enviar):
    if SELO and rotulo.startswith(("achado", "repost")):
        texto = f"{SELO}\n\n{texto}"
    print(f"\n===== POST ({rotulo}) =====\n{texto}")
    with open(ARQ_FILA, "a", encoding="utf-8") as f:
        f.write(f"### {time.strftime('%Y-%m-%d %H:%M')} | {rotulo}\n{texto}\n\n")
    if enviar:
        try:
            print(enviar_whapi(texto, imagem))
            time.sleep(PAUSA_ENTRE_POSTS)
        except BaseException as e:
            print(f"ERRO ao enviar: {e}")


FEMININO = re.compile(r"femin|\bmulher|\bvestido|\bsuti[aã]|\bcalcinha|\bbatom|\bmaquiagem", re.I)
MASCULINO = re.compile(r"masculin|\bhomem\b|\bhomens\b|\bcueca|\bbarba\b|\bbarbeador", re.I)


def publico(titulo, categoria=""):
    """feminino, masculino ou None (geral, unissex ou os dois no mesmo titulo)."""
    texto = f"{titulo} {categoria}"
    f, m = bool(FEMININO.search(texto)), bool(MASCULINO.search(texto))
    return None if f == m else "feminino" if f else "masculino"


def carregar_datas():
    try:
        with open(ARQ_DATAS, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def campanha(dia=None):
    """(campanha, "dia" ou "esquenta") de hoje, ou (None, None). O dia real vence o esquenta; entre iguais, a mais curta."""
    dia = (dia or datetime.now().date()).isoformat()
    achadas = []
    for c in carregar_datas():
        ini, fim = date.fromisoformat(c["inicio"]), date.fromisoformat(c["fim"])
        if c["inicio"] <= dia <= c["fim"]:
            achadas.append((0, (fim - ini).days, c, "dia"))
        elif c.get("esquenta", c["inicio"]) <= dia < c["inicio"]:
            achadas.append((1, (ini - date.fromisoformat(c["esquenta"])).days, c, "esquenta"))
    if not achadas:
        return None, None
    _, _, c, fase = min(achadas, key=lambda x: (x[0], x[1]))
    return c, fase


def na_campanha(titulo, categoria=""):
    """0 = combina com a campanha de hoje (palavras ou publico) e vai na frente, 1 = nao. Sem campanha, tudo 0."""
    c, _ = campanha()
    if not c or not (c.get("palavras") or c.get("publico")):
        return 0
    if c.get("palavras") and re.search(c["palavras"], f"{titulo} {categoria}", re.I):
        return 0
    return 0 if c.get("publico") and publico(titulo, categoria) == c["publico"] else 1


def avisos_do_dia(dia=None):
    """[(chave, texto)] de contagem regressiva, vespera e dia das campanhas. "vespera"/"dia" null no datas.json desliga."""
    dia = dia or datetime.now().date()
    avisos = []
    for c in carregar_datas():
        falta = (date.fromisoformat(c["inicio"]) - dia).days
        nome = c["nome"]
        if falta == 0:
            texto = c.get("dia", f"🛍️ *Hoje é {nome}!*\n\nAs melhores ofertas saem aqui o dia todo.\n\n"
                                 "🔔 Ative as notificações do Canal para não perder.")
        elif falta == 1:
            texto = c.get("vespera", f"🚨 *Amanhã é {nome}!*\n\nAs ofertas vão sair aqui o dia todo.\n\n"
                                     "🔔 Ative as notificações do Canal para não perder.")
        elif (falta > 1 and c.get("contagem") and c.get("esquenta", c["inicio"]) <= dia.isoformat()
              and falta <= c.get("contagem_dias", falta)):
            texto = (f"⏳ *Faltam {falta} dias para {c.get('artigo', 'o')} {nome}!*\n\n"
                     f"O esquenta já começou: os achados com o selo {c.get('selo_esquenta', '')} saem aqui todos os dias.\n\n"
                     "🔔 Ative as notificações do Canal para não perder.")
        else:
            continue
        if texto:
            avisos.append((f"aviso:{nome}:{dia.isoformat()}", texto))
    return avisos


def publico_da_vez(vistas):
    """Alterna mulher e homem: a vez e do publico oposto ao ultimo achado com genero.
    Campanha com publico (ex.: Dia das Maes) fixa a vez nesse publico."""
    c, _ = campanha()
    if c and c.get("publico"):
        return c["publico"]
    postados = [v for v in vistas.values() if v.get("link") and (v.get("publico") or publico(v.get("titulo", "")))]
    if not postados:
        return "feminino"
    ultimo = max(postados, key=lambda v: v.get("quando", ""))
    return "masculino" if (ultimo.get("publico") or publico(ultimo.get("titulo", ""))) == "feminino" else "feminino"


def prioridade(p, vez):
    """0 = publico da vez, 1 = o outro genero, 2 = geral."""
    return 2 if p is None else 0 if p == vez else 1


def ja_postado(vistas, link, produto, ignorar=None):
    """O Promobit pode publicar o mesmo produto como ofertas diferentes: compara o link e o produto, nao so o id."""
    limite = (datetime.now() - timedelta(days=DIAS_REPOST)).isoformat()
    return any(k != ignorar and v.get("link") and v.get("quando", "") >= limite
               and (v["link"] == link or (produto and v.get("produto") == produto))
               for k, v in vistas.items())


def desconto_real(d):
    """O desconto vale para a reserva e a repostagem so se aparecer no preco atual do ML e nao parecer inflado."""
    return bool(d["de"] and d["por"]) and DESCONTO_RESERVA <= 1 - d["por"] / d["de"] <= DESCONTO_SUSPEITO


def repostar(vistas, quantos, enviar):
    """Ultimo recurso da rodada: reposta os achados mais antigos que ainda estao com desconto."""
    limite = (datetime.now() - timedelta(days=DIAS_REPOST)).isoformat()
    antigas = [(k, v) for k, v in vistas.items()
               if v.get("link") and v.get("quando", "") < limite and v.get("loja") != "shopee"]
    feitos = 0
    vez = publico_da_vez(vistas)
    antigas.sort(key=lambda i: (prioridade(i[1].get("publico") or publico(i[1].get("titulo", "")), vez), i[1]["quando"]))
    for k, v in antigas:
        if feitos >= quantos:
            break
        try:
            d = extrair(v["link"])
        except OSError as e:
            print(f"[{k}] ERRO: {e}")
            if limite_de_pedidos(e) or not internet_ok():
                break
            continue
        except SystemExit as e:
            print(f"[{k}] repostagem: {e}; pulando")
            continue
        if not desconto_real(d) or ja_postado(vistas, v["link"], d["produto"], ignorar=k):
            continue
        v["quando"] = datetime.now().isoformat()
        v["produto"] = d["produto"]
        feitos += 1
        publicar(montar_post(d, v["link"]), d["imagem"], f"repost | {v['titulo'][:50]}", enviar)
    return feitos


def filtrar(todas, vistas, listar):
    """Separa as ofertas em candidatas (filtros normais) e folga (reserva), melhores primeiro."""
    candidatas = []
    folga = []
    for o in todas:
        motivo = None
        if o["id"] in vistas:
            motivo = "ja vista"
        elif o["desconto"] < DESCONTO_MINIMO:
            motivo = f"desconto {o['desconto'] * 100:.0f}%"
        elif o["preco"] < PRECO_MINIMO:
            motivo = "preco baixo"
        elif o["horas"] > IDADE_MAXIMA_HORAS:
            motivo = f"antiga ({o['horas']:.0f}h)"
        elif o["categoria"] in CATEGORIAS_BLOQUEADAS:
            motivo = "categoria bloqueada"
        if listar or not motivo:
            print(f"{'OK ' if not motivo else '-- '} {o['desconto'] * 100:3.0f}% R${o['preco']:>8.2f} {o['horas']:4.1f}h "
                  f"{o['categoria'][:18]:18} | {o['titulo'][:55]}" + (f"  ({motivo})" if motivo else ""))
        if not motivo:
            candidatas.append(o)
        elif (motivo.startswith(("desconto", "antiga")) and o["desconto"] >= DESCONTO_RESERVA
              and o["preco"] >= PRECO_MINIMO and o["categoria"] not in CATEGORIAS_BLOQUEADAS):
            folga.append(o)

    # melhores primeiro: mais desconto, depois mais engajamento
    ordem = lambda o: (o["desconto"], o["engajamento"])
    candidatas.sort(key=ordem, reverse=True)
    folga.sort(key=ordem, reverse=True)
    return candidatas, folga


def cota_shopee(vistas, total):
    """Com 2 ou mais posts por rodada, divide entre as lojas; com 1, reveza com o ultimo achado postado.
    No dia de campanha com "foco": "shopee" (11.11, 12.12), a Shopee fica com todos menos 1."""
    if total >= 2:
        c, fase = campanha()
        return total - 1 if c and fase == "dia" and c.get("foco") == "shopee" else total // 2
    postados = [v for v in vistas.values() if v.get("link")]
    if not postados:
        return 1
    return 0 if max(postados, key=lambda v: v.get("quando", "")).get("loja") == "shopee" else 1


def rodada_shopee(vistas, quantos, enviar):
    """Posta ate N achados da Shopee, com preco e link vindos da API. Falha aqui nao derruba a rodada do ML."""
    if quantos <= 0 or not shopee.disponivel():
        return 0
    try:
        pp = next_data(baixar(FONTE_SHOPEE, tentativas=2))
    except (OSError, RuntimeError, ValueError) as e:
        print(f"Shopee: ERRO ao ler o Promobit: {e}")
        return 0
    todas = ofertas_promobit(pp, "shopee")
    candidatas, folga = filtrar(todas, vistas, False)
    print(f"\n{len(todas)} ofertas da Shopee no Promobit, {len(candidatas)} passaram nos filtros, {len(folga)} de reserva.")
    vez = publico_da_vez(vistas)
    fila = sorted(candidatas + folga, key=lambda o: (na_campanha(o["titulo"], o["categoria"]),
                                                     prioridade(publico(o["titulo"], o["categoria"]), vez)))
    feitos = 0
    for o in fila:
        if feitos >= quantos:
            break
        vistas[o["id"]] = {"quando": datetime.now().isoformat(), "titulo": o["titulo"], "loja": "shopee"}
        try:
            m = re.search(r"https://s\.shopee\.com\.br/\w+", baixar(f"https://www.promobit.com.br/Redirect/to/{o['id']}/"))
            if not m:
                print(f"[{o['id']}] sem link da Shopee na oferta; pulando")
                continue
            loja, item = shopee.ids_do_link(m.group(0))
            d = shopee.produto(loja, item)
            if o.get("preco_atual"):
                if not desconto_real(d):
                    continue
            elif d["por"] > o["preco"] * (1 + TOLERANCIA_PRECO):
                print(f"[{o['id']}] preco subiu (Shopee: R${d['por']:.2f}, anunciado R${o['preco']:.2f}); pulando")
                if desconto_real(d):
                    fila.append({**o, "preco_atual": True})
                continue
            if ja_postado(vistas, "", d["produto"]):
                print(f"[{o['id']}] mesmo produto postado ha menos de {DIAS_REPOST} dias; pulando")
                continue
            if d["de"] and 1 - d["por"] / d["de"] > DESCONTO_SUSPEITO:
                d["de"] = None
            meu_link = shopee.gerar_link(loja, item)
        except Exception as e:
            print(f"[{o['id']}] ERRO: {e}")
            if isinstance(e, OSError) and not internet_ok():
                del vistas[o["id"]]
                break
            continue
        feitos += 1
        vistas[o["id"]].update(link=meu_link, publico=publico(o["titulo"], o["categoria"]), produto=d["produto"])
        publicar(montar_post(d, meu_link), d["imagem"], f"achado Shopee | {o['desconto'] * 100:.0f}% OFF | {o['titulo'][:50]}", enviar)
    return feitos


def achados_ml(fila, vistas, tag, lista_cupons, quantos, entregar):
    """Confere o preco no ML e gera o SEU link (Chrome logado deste PC) para ate N ofertas.
    entregar(texto, imagem, oferta) posta ou poe na fila da nuvem. Devolve (feitos, caiu)."""
    from ml_afiliado import GeradorLinks
    gerador = None
    feitos = 0
    caiu = False
    try:
        for o in fila:
            if feitos >= quantos:
                break
            vistas[o["id"]] = {"quando": datetime.now().isoformat(), "titulo": o["titulo"]}
            try:
                link_deles = link_do_promobit(o["id"])
                if not link_deles:
                    print(f"[{o['id']}] sem link do ML na oferta; pulando")
                    continue
                atual = extrair(link_deles)
                if not atual["por"] or not atual["url_produto"]:
                    print(f"[{o['id']}] produto indisponivel; pulando")
                    continue
                if o.get("preco_atual"):
                    if not desconto_real(atual):
                        continue
                elif atual["por"] > o["preco"] * (1 + TOLERANCIA_PRECO):
                    print(f"[{o['id']}] oferta acabou (ML: R${atual['por']:.2f}, anunciado R${o['preco']:.2f}); pulando")
                    if desconto_real(atual):
                        fila.append({**o, "preco_atual": True})
                    continue

                gerador = gerador or GeradorLinks(tag)
                meu_link = gerador.gerar(atual["url_produto"])
                d = extrair(meu_link)
                if d["produto"] != atual["produto"]:
                    print(f"[{o['id']}] link gerado aponta para outro produto ({d['produto']}); pulando")
                    continue
                if ja_postado(vistas, meu_link, d["produto"]):
                    print(f"[{o['id']}] mesmo produto postado ha menos de {DIAS_REPOST} dias; pulando")
                    continue
                if d["de"] and 1 - d["por"] / d["de"] > DESCONTO_SUSPEITO:
                    d["de"] = None
                # cupom da mesma categoria: so sugere o codigo (o preco do post nao desconta o cupom,
                # porque nem todo item da categoria participa)
                c = cupons.cupom_para(o, d["por"], lista_cupons)
                texto = montar_post(d, meu_link, cupom=c and c["codigo"], cupom_detalhe=c and cupons.resumo(c))
            except BaseException as e:
                print(f"[{o['id']}] ERRO: {e}")
                if limite_de_pedidos(e):
                    del vistas[o["id"]]  # a oferta nao foi avaliada: volta na proxima rodada
                    break
                if "Chrome" in str(e) or (isinstance(e, OSError) and not internet_ok()):
                    # conexao caiu (PC dormiu?): a oferta nao foi avaliada e as proximas falhariam igual.
                    # Pagina lenta de uma oferta so nao para a rodada: ela e pulada.
                    del vistas[o["id"]]
                    caiu = True
                    break
                continue

            feitos += 1
            vistas[o["id"]].update(link=meu_link, publico=publico(o["titulo"], o["categoria"]), produto=d["produto"])
            entregar(texto, d["imagem"], o)
    finally:
        if gerador:
            gerador.fechar()
    return feitos, caiu


# ---- Fila do ML para a nuvem ----

def repo_ler(caminho):
    """(texto, sha) de um arquivo do repositorio, pelo gh (o git e bloqueado neste PC). (None, None) se nao existir."""
    r = subprocess.run(["gh", "api", f"repos/{REPO}/contents/{caminho}"],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0:
        if "Not Found" in r.stdout + r.stderr:
            return None, None
        raise RuntimeError(f"gh api: {(r.stderr or r.stdout).strip()[:200]}")
    j = json.loads(r.stdout)
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def repo_gravar(caminho, texto, sha, mensagem):
    corpo = {"message": mensagem, "content": base64.b64encode(texto.encode("utf-8")).decode()}
    if sha:
        corpo["sha"] = sha
    # pelo arquivo: a linha de comando do Windows nao comporta o conteudo inteiro
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(corpo, f)
    try:
        r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{REPO}/contents/{caminho}", "--input", f.name],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        os.remove(f.name)
    if r.returncode != 0:
        raise RuntimeError(f"gh api: {(r.stderr or r.stdout).strip()[:200]}")


def sem_fila(vistas):
    """No PC, o que foi para a fila fica marcado como visto ("fila"); isso nao quer dizer postado."""
    return {k: v for k, v in vistas.items() if not v.get("fila")}


def fila_valida(fila, postadas):
    """Tira da fila o que a nuvem ja postou e o que ficou velho demais."""
    limite = (datetime.now() - timedelta(hours=FILA_HORAS)).isoformat()
    return [i for i in fila if i["quando"] >= limite and not postadas.get(i["id"], {}).get("link")]


def achados_da_fila(vistas, quantos, enviar):
    """Nuvem: posta ate N achados do ML que o PC deixou prontos, o mais recente primeiro dentro do publico da vez."""
    if quantos <= 0:
        return 0
    try:
        with open(ARQ_FILA_ML, encoding="utf-8") as f:
            fila = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        fila = []
    prontos = [i for i in fila_valida(fila, vistas) if not ja_postado(vistas, i["link"], i["produto"])]
    vez = publico_da_vez(vistas)
    prontos.sort(key=lambda i: i["quando"], reverse=True)
    prontos.sort(key=lambda i: (na_campanha(i["titulo"]), prioridade(i["publico"], vez)))
    print(f"\nFila do ML: {len(fila)} post(s), {len(prontos)} prontos para postar.")
    feitos = 0
    for i in prontos:
        if feitos >= quantos:
            break
        if ja_postado(vistas, i["link"], i["produto"]):
            continue
        vistas[i["id"]] = {"quando": datetime.now().isoformat(), "titulo": i["titulo"], "link": i["link"],
                           "publico": i["publico"], "produto": i["produto"]}
        publicar(i["texto"], i["imagem"], f"achado da fila | {i['titulo'][:50]}", enviar)
        feitos += 1
    return feitos


def rodada_nuvem(a):
    """GitHub Actions: cupom, Shopee e os achados do ML da fila. Nao usa o Chrome nem o PC."""
    agora = datetime.now()
    if a.enviar and not os.environ.get("WHAPI_TOKEN"):
        print("Falta o segredo WHAPI_TOKEN no repositorio; nada a fazer.")
        return
    if not HORARIO_NUVEM[0] <= agora.hour < HORARIO_NUVEM[1]:
        print(f"Fora do horario de postagem ({HORARIO_NUVEM[0]}h-{HORARIO_NUVEM[1]}h).")
        return
    vistas = sem_fila(carregar_vistas())
    ultima = vistas.get("rodada:nuvem", {}).get("quando")
    if a.enviar and ultima and agora - datetime.fromisoformat(ultima) < timedelta(minutes=INTERVALO_MINIMO_NUVEM):
        print(f"Ultima rodada as {ultima[11:16]}: menos de {INTERVALO_MINIMO_NUVEM} min, esta fica de fora.")
        return
    if a.enviar:
        vistas["rodada:nuvem"] = {"quando": agora.isoformat(), "titulo": "ultima rodada da nuvem"}
    global SELO
    c, fase = campanha()
    SELO = ((c.get("selo") if fase == "dia" else c.get("selo_esquenta")) or "") if c else ""
    pico = bool(c and fase == "dia" and c.get("pico"))
    teto = ACHADOS_PICO if pico else ACHADOS_POR_DIA
    if pico:
        a.max = max(a.max, ACHADOS_POR_RODADA_PICO)
    if c:
        print(f"Campanha: {c['nome']} ({fase}){' - dia de pico' if pico else ''}; selo: {SELO or '(nenhum)'}")
    hoje = agora.date().isoformat()
    achados_hoje = sum(1 for k, v in vistas.items() if v.get("link") and v.get("quando", "")[:10] == hoje)
    quantos = max(0, min(a.max, teto - achados_hoje))
    feitos = feitos_shopee = feitos_cupom = 0
    try:
        if agora.hour >= HORA_AVISOS:
            for chave, texto in avisos_do_dia():
                if chave not in vistas:
                    vistas[chave] = {"quando": agora.isoformat(), "titulo": chave}
                    publicar(texto, None, f"aviso | {chave}", a.enviar)
                    break  # um aviso por rodada; outra campanha no mesmo dia sai na proxima
        if not a.sem_cupons:
            try:
                lista_cupons = cupons.cupons_validos(next_data(baixar(FONTE, tentativas=3)))
                feitos_cupom = posts_de_cupom(lista_cupons, vistas, a.enviar, False)
            except (OSError, RuntimeError, ValueError, KeyError) as e:
                print(f"Cupons: ERRO ao ler o Promobit: {e}")
        cota = cota_shopee(vistas, quantos) if quantos else 0
        feitos_shopee = rodada_shopee(vistas, cota, a.enviar)
        feitos = achados_da_fila(vistas, quantos - feitos_shopee, a.enviar)
        # faltou achado do ML na fila: a Shopee completa
        if feitos + feitos_shopee < quantos and feitos_shopee == cota:
            feitos_shopee += rodada_shopee(vistas, quantos - feitos - feitos_shopee, a.enviar)
    finally:
        salvar_vistas(vistas)
    print(f"\n{feitos} achado(s) do ML (fila), {feitos_shopee} da Shopee e {feitos_cupom} cupom(ns). "
          f"Achados hoje: {achados_hoje + feitos + feitos_shopee}/{teto}.")


def posts_de_cupom(lista, vistas, enviar, listar):
    """Posta ate cupons.CUPONS_POR_DIA cupons novos por dia (com codigo primeiro, maior desconto primeiro)."""
    hoje = datetime.now().date().isoformat()
    de_hoje = [v["quando"] for k, v in vistas.items() if k.startswith("cupom:") and v.get("quando", "")[:10] == hoje]
    feitos_hoje = len(de_hoje)
    novos = [c for c in lista if cupons.vale_post(c) and f"cupom:{c['codigo'] or c['id']}" not in vistas]
    novos.sort(key=lambda c: (bool(c["codigo"]), c["pct"] or 0, c["fixo"] or 0), reverse=True)
    print(f"\nCupons: {len(lista)} validos, {len(novos)} novos para post, {feitos_hoje} ja postados hoje "
          f"(limite {cupons.CUPONS_POR_DIA}/dia)")
    if listar:
        for c in novos:
            print(f"   {c['codigo'] or '(automatico)':16} {cupons.resumo(c):34} {c['categoria']}")
        return 0
    feitos = 0
    if de_hoje and datetime.now() - datetime.fromisoformat(max(de_hoje)) < timedelta(hours=cupons.INTERVALO_CUPOM_HORAS):
        return 0
    for c in novos[:min(cupons.CUPONS_POR_RODADA, max(0, cupons.CUPONS_POR_DIA - feitos_hoje))]:
        vistas[f"cupom:{c['codigo'] or c['id']}"] = {"quando": datetime.now().isoformat(), "titulo": c["titulo"]}
        publicar(cupons.post_cupom(c, LINK_CUPONS), None, f"cupom | {c['codigo'] or 'automatico'}", enviar)
        feitos += 1
    return feitos


def main():
    ap = argparse.ArgumentParser(description="Descobre ofertas do ML e gera posts com o seu link")
    ap.add_argument("--tag", default=os.environ.get("ML_TAG"), help="etiqueta de afiliado (ou ML_TAG)")
    ap.add_argument("--enviar", action="store_true", help="posta no Canal via Whapi")
    ap.add_argument("--listar", action="store_true", help="so lista as ofertas e cupons, sem gerar links")
    ap.add_argument("--max", type=int, default=MAX_POSTS_POR_RODADA, help="maximo de posts nesta execucao")
    ap.add_argument("--sem-cupons", action="store_true", help="nao gera posts proprios de cupom")
    ap.add_argument("--abastecer", action="store_true", help="PC: gera os links do ML e deixa os posts na fila da nuvem")
    ap.add_argument("--nuvem", action="store_true", help="GitHub Actions: cupom, Shopee e os posts do ML da fila")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.enviar and datetime.now().date().isoformat() >= DOBRAR_DESDE:
        a.max *= 2
        cupons.CUPONS_POR_DIA *= 2
        cupons.INTERVALO_CUPOM_HORAS = 1.1  # rodadas a cada 35 min: 1 cupom a cada 2 rodadas
    if a.nuvem:
        return rodada_nuvem(a)

    preparar_ao_acordar()
    vistas = carregar_vistas()
    if a.abastecer:
        # o registro da nuvem vale: e ela quem posta
        texto, _ = repo_ler(REPO_VISTAS)
        postadas = sem_fila(json.loads(texto) if texto else {})
        vistas.update(postadas)
        texto, sha_fila = repo_ler(REPO_FILA_ML)
        fila_antes = json.loads(texto) if texto else []
        fila_ml = fila_valida(fila_antes, postadas)
        a.max = max(0, min(a.max, FILA_ALVO - len(fila_ml)))
        print(f"Fila da nuvem: {len(fila_ml)} post(s) do ML prontos (alvo {FILA_ALVO}); gerando ate {a.max}.")
    pp = next_data(baixar(FONTE, tentativas=3))
    lista_cupons = cupons.cupons_validos(pp)
    todas = ofertas_promobit(pp)
    candidatas, folga = filtrar(todas, vistas, a.listar)
    print(f"\n{len(todas)} ofertas do ML no Promobit, {len(candidatas)} passaram nos filtros, {len(folga)} de reserva.")
    if a.listar:
        for o in candidatas:
            c = cupons.cupom_para(o, o["preco"], lista_cupons)
            if c:
                print(f"   cupom para '{o['titulo'][:40]}': {c['codigo']} ({cupons.resumo(c)})")
        posts_de_cupom(lista_cupons, vistas, False, True)
        return
    # genero primeiro, alternando mulher e homem; a ordem de desconto e mantida dentro de cada grupo.
    # A lista cresce no laco: ofertas que subiram de preco voltam no fim.
    vez = publico_da_vez(vistas)
    fila = sorted(candidatas + folga, key=lambda o: (na_campanha(o["titulo"], o["categoria"]),
                                                     prioridade(publico(o["titulo"], o["categoria"]), vez)))
    if fila and not a.tag:
        raise SystemExit("Informe --tag ou defina ML_TAG.")

    if a.abastecer:
        def enfileirar(texto, imagem, o):
            v = vistas[o["id"]]
            v["fila"] = True
            fila_ml.append({"id": o["id"], "quando": v["quando"], "titulo": o["titulo"], "texto": texto, "imagem": imagem,
                            "link": v["link"], "produto": v["produto"], "publico": v["publico"]})
            print(f"\n===== NA FILA DA NUVEM ({o['desconto'] * 100:.0f}% OFF | {o['titulo'][:50]}) =====\n{texto}")

        try:
            feitos = achados_ml(fila, vistas, a.tag, lista_cupons, a.max, enfileirar)[0] if a.max else 0
            if feitos or len(fila_ml) != len(fila_antes):
                repo_gravar(REPO_FILA_ML, json.dumps(fila_ml, ensure_ascii=False, indent=1), sha_fila,
                            f"Fila do canal: {len(fila_ml)} post(s) do ML")
        finally:
            salvar_vistas(vistas)
        print(f"\n{feitos} post(s) do ML na fila; fila com {len(fila_ml)}.")
        return

    feitos = feitos_shopee = feitos_cupom = reposts = 0
    caiu = False
    try:
        if not a.sem_cupons:
            feitos_cupom = posts_de_cupom(lista_cupons, vistas, a.enviar, False)
        cota = cota_shopee(vistas, a.max)
        feitos_shopee = rodada_shopee(vistas, cota, a.enviar)
        feitos, caiu = achados_ml(fila, vistas, a.tag, lista_cupons, a.max - feitos_shopee, lambda texto, imagem, o: publicar(
            texto, imagem, f"achado | {o['desconto'] * 100:.0f}% OFF | {o['titulo'][:50]}", a.enviar))
        # faltou achado do ML: a Shopee completa, se ainda nao tiver esgotado as dela nesta rodada
        if feitos + feitos_shopee < a.max and not caiu and feitos_shopee == cota:
            feitos_shopee += rodada_shopee(vistas, a.max - feitos - feitos_shopee, a.enviar)
        if feitos + feitos_shopee < a.max and not caiu:
            reposts = repostar(vistas, a.max - feitos - feitos_shopee, a.enviar)
    finally:
        salvar_vistas(vistas)
    print(f"\n{feitos} achado(s) do ML, {feitos_shopee} da Shopee, {reposts} repost(s) e {feitos_cupom} cupom(ns) gerados. "
          "Fila em fila_posts.txt")


if __name__ == "__main__":
    main()
