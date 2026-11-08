"""
Postagem no Canal do WhatsApp via Whapi.Cloud (nao oficial; use um numero dedicado).

Configuracao (uma vez):
  1. Crie a conta em whapi.cloud, abra o canal padrao no painel e escaneie o QR code
     com o WhatsApp do numero que ADMINISTRA o seu Canal (Aparelhos conectados).
  2. Copie o token do painel e crie o arquivo whapi.json nesta pasta:
       {"token": "SEU_TOKEN", "canal": ""}
  3. Descubra o ID do Canal:
       python whapi.py --canais
     e coloque o ID (termina em @newsletter) no campo "canal" do whapi.json.
  4. Teste postando UM post no Canal (vai aparecer para os seguidores):
       python gerar_post.py https://meli.la/SEU_LINK --enviar

O whapi.json guarda o seu token: nao compartilhe esse arquivo.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_CONFIG = os.path.join(PASTA, "whapi.json")
BASE = "https://gate.whapi.cloud"
# Smart App Control bloqueia bibliotecas de imagem no Python; o System.Drawing do Windows ja vem instalado.
QUADRAR_PS = r"""
Add-Type -AssemblyName System.Drawing
$foto = [Drawing.Image]::FromFile($env:FOTO_ENTRADA)
if ($foto.Width -eq $foto.Height) { $foto.Dispose(); exit 0 }
$lado = [Math]::Max($foto.Width, $foto.Height)
$tela = New-Object Drawing.Bitmap $lado, $lado
$g = [Drawing.Graphics]::FromImage($tela)
$g.Clear([Drawing.Color]::White)
$g.DrawImage($foto, [int](($lado - $foto.Width) / 2), [int](($lado - $foto.Height) / 2), $foto.Width, $foto.Height)
$jpeg = [Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$qualidade = New-Object Drawing.Imaging.EncoderParameters 1
$qualidade.Param[0] = New-Object Drawing.Imaging.EncoderParameter ([Drawing.Imaging.Encoder]::Quality), 90L
$tela.Save($env:FOTO_SAIDA, $jpeg, $qualidade)
$g.Dispose(); $tela.Dispose(); $foto.Dispose()
"""


def config():
    cfg = {}
    if os.path.exists(ARQ_CONFIG):
        with open(ARQ_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["token"] = os.environ.get("WHAPI_TOKEN") or cfg.get("token", "")
    cfg["canal"] = os.environ.get("WHAPI_CANAL") or cfg.get("canal", "")
    cfg["modo"] = os.environ.get("WHAPI_MODO") or cfg.get("modo", "imagem")  # link | imagem | texto
    if not cfg["token"]:
        raise SystemExit("Falta o token do Whapi. Crie o whapi.json (veja o topo do whapi.py).")
    return cfg


def chamar(metodo, caminho, corpo=None):
    cfg = config()
    req = urllib.request.Request(
        BASE + caminho, method=metodo,
        data=json.dumps(corpo).encode() if corpo is not None else None,
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "ComparaWhey/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode(errors="replace")[:400]
        if e.code == 401:
            raise RuntimeError("Token do Whapi invalido ou expirado (401).") from None
        if e.code == 429:
            raise RuntimeError("WhatsApp limitou o envio (429). Espere e poste com menos frequencia.") from None
        raise RuntimeError(f"Whapi respondeu {e.code}: {detalhe}") from None


def existe(url):
    """O mlstatic responde 200 com um GIF generico quando a imagem nao existe: so JPEG conta."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ComparaWhey/1.0"}), timeout=20) as r:
            return r.headers.get("Content-Type", "").startswith("image/jpeg")
    except OSError:
        return False


def imagem_jpg(url):
    """O ML entrega miniatura .webp (-P, 250px); o Whapi aceita JPG/PNG. O mlstatic serve a mesma imagem em -F.jpg (1000px)."""
    if url and "mlstatic.com" in url:
        grande = re.sub(r"-[A-Z]\.\w+$", "-F.jpg", url)
        if grande != url and existe(grande):
            return grande
        if url.endswith(".webp"):
            return url[:-5] + ".jpg"
    return url


def _quadrar_pil(entrada, saida):
    """Na nuvem (Linux) com Pillow: mesmo resultado do QUADRAR_PS."""
    from PIL import Image
    with Image.open(entrada) as foto:
        if foto.width == foto.height:
            return
        lado = max(foto.size)
        tela = Image.new("RGB", (lado, lado), "white")
        tela.paste(foto.convert("RGB"), ((lado - foto.width) // 2, (lado - foto.height) // 2))
        tela.save(saida, "JPEG", quality=90)


def foto_quadrada(url):
    """O card do WhatsApp segue o formato da foto: completa com branco ate 1:1, sem cortar o produto.
    Devolve base64 pronto para o campo media, ou None se ja for quadrada ou nao der para converter."""
    with tempfile.TemporaryDirectory() as pasta:
        entrada, saida = os.path.join(pasta, "entrada"), os.path.join(pasta, "saida.jpg")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ComparaWhey/1.0"}), timeout=20) as r:
                with open(entrada, "wb") as f:
                    f.write(r.read())
            try:
                import PIL  # noqa: F401
            except ImportError:
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", QUADRAR_PS],
                               env={**os.environ, "FOTO_ENTRADA": entrada, "FOTO_SAIDA": saida},
                               capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                _quadrar_pil(entrada, saida)
            with open(saida, "rb") as f:
                return "data:image/jpeg;name=foto.jpg;base64," + base64.b64encode(f.read()).decode()
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def postar(texto, imagem=None):
    """Posta no Canal. Modo link: card com foto que abre o link + texto. Modo imagem: foto + legenda. Sem imagem: so texto."""
    cfg = config()
    if not cfg["canal"].endswith("@newsletter"):
        raise SystemExit('Falta o ID do Canal no whapi.json ("canal": "...@newsletter"). Rode: python whapi.py --canais')
    if cfg["modo"] == "link" and imagem and "https://" in texto:
        # O Whapi exige title; em branco o card mostra so a foto e o dominio (meli.la).
        card = {"to": cfg["canal"], "body": texto, "title": " "}
        original = imagem_jpg(imagem)
        quadrada = foto_quadrada(original)
        if quadrada:
            try:
                return chamar("POST", "/messages/link_preview", {**card, "media": quadrada})
            except RuntimeError as e:
                print(f"Foto quadrada recusada ({e}); enviando a original.")
        return chamar("POST", "/messages/link_preview", {**card, "media": original})
    if cfg["modo"] != "texto" and imagem:
        return chamar("POST", "/messages/image", {"to": cfg["canal"], "media": imagem_jpg(imagem), "caption": texto})
    # Sem foto propria, o WhatsApp usaria a imagem da pagina do link: no perfil social, a de um produto qualquer.
    return chamar("POST", "/messages/text", {"to": cfg["canal"], "body": texto, "no_link_preview": True})


def listar_canais():
    r = chamar("GET", "/newsletters?count=100")
    canais = r.get("newsletters", r if isinstance(r, list) else [])
    if not canais:
        print("Nenhum Canal encontrado. O numero conectado no Whapi e administrador do Canal?")
        print(json.dumps(r, ensure_ascii=False)[:500])
    for c in canais:
        print(f"{c.get('id')}  |  {c.get('name')}  |  papel: {c.get('role', '?')}  |  seguidores: {c.get('subscribers_count', '?')}")


def main():
    ap = argparse.ArgumentParser(description="Whapi.Cloud: listar Canais e testar a conexao")
    ap.add_argument("--canais", action="store_true", help="lista os Canais do numero conectado")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.canais:
        listar_canais()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
