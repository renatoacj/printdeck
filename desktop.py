"""Aplicativo de desktop do PrintDeck (fila de impressão 3D compartilhada).

O programa (servidor, fila, OctoPrint, link da internet) roda dentro desta janela, sem terminal:
  - minimizar é o minimizar normal do Windows (fica na barra de tarefas);
  - fechar (X) manda para a bandeja, perto do relógio, e ele continua funcionando (dá para desligar isso nas
    Configurações); "Fechar programa" no menu da bandeja desliga de verdade.

Abrir:  pythonw desktop.py            (o atalho "PrintDeck" faz isso)
        pythonw desktop.py --bandeja  (começa escondido na bandeja; usado ao iniciar com o Windows)
        python  desktop.py --atalho   (cria o atalho na Área de Trabalho)
"""
import ctypes
import os
import subprocess
import sys
import threading
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
NOME = "PrintDeck"
PASTA_LOCAL = os.path.join(os.path.expanduser("~"), "PrintDeck")
os.makedirs(PASTA_LOCAL, exist_ok=True)
ICONE = os.path.join(PASTA_LOCAL, "impressora.ico")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


VAZIA = "<html><body style='background:#111110'></body></html>"


def aviso(texto, erro=False):
    ctypes.windll.user32.MessageBoxW(0, texto, NOME, 0x10 if erro else 0x40)


def desenhar_icone(lado=256):
    """Impressora branca num quadrado azul (a cor do tema do sistema)."""
    from PIL import Image, ImageDraw
    k = lado / 64
    img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = lambda x0, y0, x1, y1, raio, cor: d.rounded_rectangle([x0 * k, y0 * k, x1 * k, y1 * k], radius=raio * k, fill=cor)
    r(2, 2, 62, 62, 14, "#2a78d6")
    r(21, 13, 43, 27, 2, "#ffffff")          # papel entrando
    r(12, 24, 52, 45, 5, "#ffffff")          # corpo
    r(21, 37, 43, 52, 2, "#ffffff")          # peça saindo
    d.rounded_rectangle([21 * k, 37 * k, 43 * k, 52 * k], radius=2 * k, outline="#2a78d6", width=max(1, round(2 * k)))
    d.ellipse([43 * k, 29 * k, 47 * k, 33 * k], fill="#2a78d6")
    return img


def garantir_icone():
    if not os.path.exists(ICONE):
        desenhar_icone().save(ICONE, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return ICONE


def pasta_do_windows(nome):
    """'Desktop' ou 'Startup' (a pasta de programas que abrem junto com o Windows)."""
    saida = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{nome}')"],
                           capture_output=True, text=True, creationflags=0x08000000)
    return saida.stdout.strip()


def criar_atalho(pasta, argumentos=""):
    garantir_icone()
    destino = os.path.join(pasta, NOME + ".lnk")
    script = ("$a = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ATALHO); $a.TargetPath = $env:ALVO; "
              "$a.Arguments = $env:ARGS_ATALHO; $a.WorkingDirectory = $env:PASTA; $a.IconLocation = $env:ICONE; "
              "$a.Description = 'Fila de impressão 3D dos amigos'; $a.Save()")
    amb = dict(os.environ, ATALHO=destino, ALVO=PYTHONW, ARGS_ATALHO=f'"{os.path.join(BASE, "desktop.py")}" {argumentos}'.strip(),
               PASTA=BASE, ICONE=ICONE)
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=amb, creationflags=0x08000000, check=True)
    return destino


def principal():
    comeca_na_bandeja = "--bandeja" in sys.argv
    if sys.stdout is None or "pythonw" in os.path.basename(sys.executable).lower():
        # sem terminal: o que seria impresso vai para um arquivo (útil se algo der errado)
        log = os.path.join(PASTA_LOCAL, "programa.log")
        if os.path.exists(log) and os.path.getsize(log) > 2_000_000:
            os.remove(log)
        sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("printdeck.app")
    except Exception:
        pass

    import logging
    import pystray
    import webview
    from werkzeug.serving import make_server
    import app as A

    if A.ja_esta_rodando(A.PORTA):
        aviso("O programa já está aberto.\n\nProcure o ícone da impressora na bandeja, perto do relógio "
              "(ou feche a janela preta do “iniciar.bat”, se ele estiver aberto por lá).")
        return

    # ---- o mesmo que o "python app.py" faz, só que dentro deste programa
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    with A.app.app_context():
        cfg = A.get_settings()
        if cfg["internet_ligada"] == "1" and not A.senha_admin_padrao(cfg):
            A.ligar_internet()
        entrada = A.app.test_request_context()
        with entrada:
            caminho = A.url_for("admin_link", token=A.link_fixo_admin(A.get_db()))
    A.iniciar_sincronizacao()
    servidor = make_server("0.0.0.0", A.PORTA, A.app, threaded=True)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    endereco = f"http://127.0.0.1:{A.PORTA}"

    # ---- janela (já entra no Administrativo: este computador é o do dono)
    webview.settings["ALLOW_DOWNLOADS"] = True
    # começando na bandeja, a página nem é carregada: só quando a janela for aberta
    janela = webview.create_window(NOME, None if comeca_na_bandeja else endereco + caminho,
                                   html=VAZIA if comeca_na_bandeja else None, width=1500, height=940,
                                   min_size=(420, 560), hidden=comeca_na_bandeja, confirm_close=False,
                                   text_select=True, background_color="#111110")
    estado = {"avisou": False, "saindo": False, "descarregada": comeca_na_bandeja}

    def fechar_minimiza():
        return A.ler_config("fechar_minimiza") != "0"

    def alternar_fechar(*_):
        import sqlite3
        db = sqlite3.connect(A.DB_PATH)
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('fechar_minimiza', ?)",
                   ("0" if fechar_minimiza() else "1",))
        db.commit()
        db.close()

    def ao_fechar(*_):
        """X da janela: com a opção ligada, só esconde na bandeja (sem aviso) e o programa continua."""
        if estado["saindo"]:
            return True
        if fechar_minimiza():
            threading.Thread(target=para_a_bandeja, daemon=True).start()
            return False
        janela.confirm_close = True
        return True

    def abrir(*_):
        if estado["descarregada"]:
            estado["descarregada"] = False
            janela.load_url(endereco + caminho)
        janela.show()
        janela.restore()

    def para_a_bandeja(*_):
        janela.hide()
        # na bandeja a página é descarregada: o navegador embutido para de consultar, de desenhar o 3D e
        # devolve a memória. O servidor (fila, OctoPrint, link) continua funcionando normalmente.
        estado["descarregada"] = True
        janela.load_html(VAZIA)
        if not estado["avisou"]:
            estado["avisou"] = True
            try:
                bandeja.notify("Continuo funcionando aqui na bandeja. Clique no ícone para abrir de novo.", NOME)
            except Exception:
                pass

    def sair(*_):
        estado["saindo"] = True
        janela.confirm_close = False
        janela.destroy()

    def por_icone_na_janela(*_):
        try:
            hwnd = janela.native.Handle.ToInt64()
            for tamanho, qual in ((16, 0), (32, 1)):
                h = ctypes.windll.user32.LoadImageW(None, garantir_icone(), 1, tamanho, tamanho, 0x10)
                ctypes.windll.user32.SendMessageW(ctypes.c_void_p(hwnd), 0x80, qual, ctypes.c_void_p(h))
        except Exception as e:
            print("Não consegui trocar o ícone da janela:", e)

    janela.events.closing += ao_fechar
    janela.events.shown += por_icone_na_janela

    # ---- ícone na bandeja
    atalho_inicio = os.path.join(pasta_do_windows("Startup"), NOME + ".lnk")

    def alternar_inicio(*_):
        if os.path.exists(atalho_inicio):
            os.remove(atalho_inicio)
        else:
            criar_atalho(os.path.dirname(atalho_inicio), "--bandeja")

    bandeja = pystray.Icon("printdeck", desenhar_icone(64), NOME, menu=pystray.Menu(
        pystray.MenuItem("Abrir", abrir, default=True),
        pystray.MenuItem("Abrir no navegador", lambda *_: webbrowser.open(endereco + caminho)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Ao fechar a janela, minimizar para a bandeja", alternar_fechar, checked=lambda _: fechar_minimiza()),
        pystray.MenuItem("Abrir junto com o Windows", alternar_inicio, checked=lambda _: os.path.exists(atalho_inicio)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Fechar programa", sair)))
    bandeja.run_detached()

    webview.start(private_mode=False, storage_path=os.path.join(PASTA_LOCAL, "janela"), localization={
        "global.quitConfirmation": "Fechar desliga o programa: os amigos ficam sem a fila e a impressão deixa de ser "
                                   "acompanhada.\n(Para deixar funcionando, use o botão de minimizar.)\n\nFechar mesmo?"})

    # janela fechada = programa desligado (o link fixo sai do ar junto)
    try:
        A.tunel.ao_fechar_programa()
    except Exception:
        pass
    try:
        bandeja.stop()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    if "--atalho" in sys.argv:
        print("Atalho criado em:", criar_atalho(pasta_do_windows("Desktop")))
        print("Atalho criado em:", criar_atalho(pasta_do_windows("Programs")))   # menu Iniciar
    else:
        try:
            principal()
        except Exception as e:
            import traceback
            traceback.print_exc()
            aviso(f"Não consegui abrir o programa:\n\n{e}", erro=True)
            os._exit(1)
