"""Túnel rápido da Cloudflare (trycloudflare.com): deixa a fila acessível pela internet
sem conta, sem domínio e sem abrir portas no roteador.

O link só muda quando o cloudflared é reiniciado. Se o programa da fila for fechado no X (ou
reiniciado) e o cloudflared continuar rodando, o programa novo reaproveita o mesmo túnel,
e o link dos amigos continua igual."""
import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

# nomes de túnel rápido são sempre palavras-com-hifen (evita pegar api.trycloudflare.com dos logs)
_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
_CANDIDATOS = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
]
# endereço de métricas do cloudflared, onde ele informa o link atual; as portas 20241-20245
# são as que ele escolhe sozinho (túneis abertos por versões anteriores do programa)
PORTA_METRICAS = 20299
# sem isto, cada consulta abre e fecha uma janela de terminal quando o programa roda sem console (aplicativo)
_SEM_JANELA = 0x08000000 if os.name == "nt" else 0
_PORTAS_METRICAS = [PORTA_METRICAS, 20241, 20242, 20243, 20244, 20245]


def _cloudflared_vivo(pid):
    try:
        if os.name == "nt":
            saida = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                   capture_output=True, text=True, creationflags=_SEM_JANELA).stdout
            return "cloudflared" in saida.lower()
        with open(f"/proc/{pid}/comm") as f:
            return "cloudflared" in f.read()
    except OSError:
        return False


def _matar(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=_SEM_JANELA)
        else:
            os.kill(pid, 15)
    except OSError:
        pass


def _link_nas_metricas():
    """Pergunta ao cloudflared que já está rodando qual é o link dele."""
    for porta in _PORTAS_METRICAS:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/quicktunnel", timeout=1) as r:
                host = json.load(r).get("hostname")
            if host:
                return "https://" + host
        except (OSError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- link fixo pelo Tailscale Funnel
# Quem tem o Tailscale instalado e com o Funnel liberado ganha um endereço que nunca muda
# (https://<nome-do-pc>.<rede>.ts.net). Os amigos abrem como um site comum, sem instalar nada.

def _tailscale():
    return shutil.which("tailscale") or next((c for c in (r"C:\Program Files\Tailscale\tailscale.exe",
                                                          r"C:\Program Files (x86)\Tailscale\tailscale.exe",
                                                          "/usr/bin/tailscale", "/usr/local/bin/tailscale",
                                                          "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
                                              if os.path.exists(c)), None)


def _rodar_tailscale(*args, timeout=25):
    return subprocess.run([_tailscale(), *args], capture_output=True, text=True, encoding="utf-8", errors="ignore",
                          timeout=timeout, creationflags=_SEM_JANELA)


def _estado_tailscale():
    """(situação, endereço). Situação: "sem" (não instalado), "desligado" (instalado, mas sem login/desconectado),
    "sem_funnel" (logado, mas o Funnel não está liberado na conta) ou "pronto"."""
    if not _tailscale():
        return "sem", None
    try:
        eu = json.loads(_rodar_tailscale("status", "--json", timeout=8).stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "desligado", None
    proprio = eu.get("Self") or {}
    nome = (proprio.get("DNSName") or "").rstrip(".")
    if eu.get("BackendState") != "Running" or not nome:
        return "desligado", None
    if "funnel" not in (proprio.get("CapMap") or {}):
        return "sem_funnel", None
    return "pronto", "https://" + nome


def situacao_tailscale():
    return _estado_tailscale()[0]


def endereco_funnel():
    """Endereço fixo deste computador no Funnel, ou None se o Tailscale não estiver pronto para isso."""
    return _estado_tailscale()[1]


def _ler_pid(pid_file):
    try:
        with open(pid_file) as f:
            return int(f.read())
    except (OSError, ValueError, TypeError):
        return None


class Tunel:
    def __init__(self):
        self.pid_file = None       # definido pelo app
        self.ao_mudar_link = None  # função(url) chamada quando sai um link novo
        self.proc = None           # cloudflared aberto por este programa
        self.pid_adotado = None    # cloudflared que já estava rodando e foi reaproveitado
        self.url = None
        self.erro = None
        self.modo = "auto"         # "auto" = link fixo do Tailscale se der, senão Cloudflare; "cloudflare" = só Cloudflare
        self.fixo = False          # True quando o link no ar é o fixo (Tailscale Funnel)
        self._lock = threading.Lock()

    @staticmethod
    def executavel():
        return shutil.which("cloudflared") or next((c for c in _CANDIDATOS if os.path.exists(c)), None)

    @property
    def ligado(self):
        return self.fixo or (self.proc is not None and self.proc.poll() is None) or self.pid_adotado is not None

    def _ligar_funnel(self, porta):
        """Liga o link fixo. Devolve False (sem mexer em nada) se o Tailscale não estiver disponível."""
        if os.environ.get("SEM_LINK_FIXO"):      # servidores de teste nunca publicam nada no endereço fixo
            return False
        url = endereco_funnel() if self.modo == "auto" else None
        if not url:
            return False
        try:
            r = _rodar_tailscale("funnel", "--bg", "--yes", str(porta))
        except (OSError, subprocess.TimeoutExpired) as e:
            print("Tailscale Funnel não ligou:", e, flush=True)
            return False
        if r.returncode != 0:
            print("Tailscale Funnel não ligou:", (r.stderr or r.stdout).strip()[:400], flush=True)
            return False
        # um túnel antigo do Cloudflare não precisa mais ficar no ar (seria um segundo endereço público)
        velho = _ler_pid(self.pid_file)
        if velho and _cloudflared_vivo(velho):
            _matar(velho)
        if self.pid_file and os.path.exists(self.pid_file):
            os.remove(self.pid_file)
        self.fixo, self.url = True, url
        print(f"\n  >>> Link fixo para os amigos: {url}\n", flush=True)
        return True

    def _desligar_funnel(self):
        try:
            _rodar_tailscale("funnel", "--https=443", "off")
        except (OSError, subprocess.TimeoutExpired) as e:
            print("Não consegui desligar o Tailscale Funnel:", e, flush=True)

    def ao_fechar_programa(self):
        """Programa fechando: o link fixo sai do ar junto (o endereço continua o mesmo na próxima vez).
        O túnel do Cloudflare fica, para o link dele não mudar."""
        if self.fixo:
            self.fixo, self.url = False, None
            self._desligar_funnel()

    def ligar(self, porta):
        with self._lock:
            if self.ligado:
                return
            self.url, self.erro = None, None
            if self._ligar_funnel(porta):
                return
            if self._adotar():
                return
            exe = self.executavel()
            if not exe:
                self.erro = "cloudflared não encontrado. Instale com: winget install Cloudflare.cloudflared"
                return
            self.proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--metrics", f"127.0.0.1:{PORTA_METRICAS}",
                 "--url", f"http://localhost:{porta}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="ignore",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if self.pid_file:
                with open(self.pid_file, "w") as f:
                    f.write(str(self.proc.pid))
            threading.Thread(target=self._ler_saida, args=(self.proc,), daemon=True).start()

    def _adotar(self):
        """Reaproveita o cloudflared da execução anterior (mesmo link). Se não der, encerra ele."""
        pid = _ler_pid(self.pid_file)
        if not pid or not _cloudflared_vivo(pid):
            return False
        url = _link_nas_metricas()
        if not url:
            _matar(pid)
            return False
        self.pid_adotado, self.url = pid, url
        print(f"\n  >>> Link para os amigos (o mesmo de antes): {url}\n", flush=True)
        threading.Thread(target=self._vigiar_adotado, args=(pid,), daemon=True).start()
        return True

    def _vigiar_adotado(self, pid):
        while self.pid_adotado == pid:
            time.sleep(10)
            if self.pid_adotado == pid and not _cloudflared_vivo(pid):
                self.pid_adotado, self.url = None, None
                self.erro = "O túnel parou (sem internet?). Clique em ligar de novo."

    def _ler_saida(self, proc):
        for linha in proc.stderr:
            m = _URL_RE.search(linha)
            if m and proc is self.proc and not self.url:
                self.url = m.group()
                print(f"\n  >>> Link para os amigos: {self.url}\n", flush=True)
                if self.ao_mudar_link:
                    try:
                        self.ao_mudar_link(self.url)
                    except Exception as e:  # um aviso que falhou não pode derrubar o túnel
                        print("Não consegui avisar o link novo:", e, flush=True)
        if proc is self.proc:  # terminou sem ter sido desligado pelo painel
            self.url = None
            self.erro = "O túnel parou (sem internet?). Clique em ligar de novo."

    def desligar(self):
        with self._lock:
            if self.fixo:
                self.fixo = False
                self._desligar_funnel()
            proc, adotado = self.proc, self.pid_adotado
            self.proc, self.pid_adotado, self.url = None, None, None
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if adotado:
                _matar(adotado)
            if (proc or adotado) and self.pid_file and os.path.exists(self.pid_file):
                os.remove(self.pid_file)


tunel = Tunel()
atexit.register(tunel.desligar)
