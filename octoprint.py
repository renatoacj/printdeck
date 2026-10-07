"""Cliente mínimo da API REST do OctoPrint (https://docs.octoprint.org/en/master/api/)."""
import requests


class OctoPrintErro(Exception):
    pass


class OctoPrint:
    def __init__(self, url, api_key, timeout=5):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _req(self, metodo, caminho, timeout=None, **kw):
        try:
            r = requests.request(metodo, self.url + caminho, headers={"X-Api-Key": self.api_key},
                                 timeout=timeout or self.timeout, **kw)
        except requests.RequestException:
            raise OctoPrintErro(f"Não consegui falar com o OctoPrint em {self.url}. O Raspberry está ligado?")
        if r.status_code in (401, 403):
            raise OctoPrintErro("Chave de API recusada pelo OctoPrint. Confira a chave.")
        return r

    def versao(self):
        r = self._req("GET", "/api/version")
        if r.status_code != 200:
            raise OctoPrintErro(f"Resposta inesperada do OctoPrint ({r.status_code}). O endereço está certo?")
        return r.json()

    def estado(self):
        """Resumo do estado da impressora e do trabalho atual."""
        r = self._req("GET", "/api/printer")
        if r.status_code == 409:  # OctoPrint ligado, mas sem conexão com a impressora
            return {"online": True, "conectada": False, "texto": "Impressora desconectada",
                    "imprimindo": False, "pausada": False, "ocupada": False, "pronta": False}
        if r.status_code != 200:
            raise OctoPrintErro(f"Erro ao ler a impressora ({r.status_code}).")
        p = r.json()
        flags = p.get("state", {}).get("flags", {})
        temp = p.get("temperature", {})
        job = self._req("GET", "/api/job").json()
        prog = job.get("progress") or {}
        imprimindo = bool(flags.get("printing"))
        pausada = bool(flags.get("paused") or flags.get("pausing"))
        ocupada = imprimindo or pausada or bool(flags.get("cancelling")) or bool(flags.get("finishing"))
        return {
            "online": True,
            "conectada": True,
            "texto": _traduz(p.get("state", {}).get("text", "")),
            "imprimindo": imprimindo,
            "pausada": pausada,
            "ocupada": ocupada,
            "pronta": bool(flags.get("ready") or flags.get("operational")) and not ocupada
                      and not flags.get("error"),
            "temp_bico": (temp.get("tool0") or {}).get("actual"),
            "alvo_bico": (temp.get("tool0") or {}).get("target"),
            "temp_mesa": (temp.get("bed") or {}).get("actual"),
            "alvo_mesa": (temp.get("bed") or {}).get("target"),
            "arquivo": ((job.get("job") or {}).get("file") or {}).get("name"),
            "progresso": prog.get("completion"),
            "decorrido_s": prog.get("printTime"),
            "restante_s": prog.get("printTimeLeft"),
            "filepos": prog.get("filepos"),  # byte do G-code que está sendo impresso
        }

    def conectar(self):
        self._req("POST", "/api/connection", json={"command": "connect"})

    def enviar_e_imprimir(self, caminho, nome):
        """Envia o G-code e começa a imprimir. Retorna o nome com que o OctoPrint salvou."""
        with open(caminho, "rb") as f:
            r = self._req("POST", "/api/files/local", timeout=300,
                          files={"file": (nome, f, "application/octet-stream")},
                          data={"select": "true", "print": "true"})
        if r.status_code == 409:
            raise OctoPrintErro("O OctoPrint recusou: a impressora está desconectada ou já imprimindo.")
        if r.status_code not in (200, 201):
            raise OctoPrintErro(f"O OctoPrint recusou o arquivo ({r.status_code}).")
        return ((r.json().get("files") or {}).get("local") or {}).get("name") or nome

    def comando(self, comando, acao=None):
        corpo = {"command": comando}
        if acao:
            corpo["action"] = acao
        r = self._req("POST", "/api/job", json=corpo)
        if r.status_code == 409:
            raise OctoPrintErro("Não há impressão em andamento para isso.")

    def gcode(self, comandos):
        """Manda comandos G-code direto para a impressora (ex.: ["G28 X Y"])."""
        r = self._req("POST", "/api/printer/command", json={"commands": list(comandos)})
        if r.status_code == 409:
            raise OctoPrintErro("A impressora não está pronta para receber comandos.")
        if r.status_code not in (200, 204):
            raise OctoPrintErro(f"O OctoPrint recusou o comando ({r.status_code}).")

    def ultimo_resultado(self, nome):
        """{'success': bool, 'date': epoch, 'printTime': s} da última vez que o arquivo foi impresso."""
        r = self._req("GET", f"/api/files/local/{requests.utils.quote(nome)}")
        if r.status_code != 200:
            return None
        return (r.json().get("prints") or {}).get("last")

    def foto_webcam(self, url_foto=None):
        try:
            r = requests.get(url_foto or self.url + "/webcam/?action=snapshot", timeout=5)
        except requests.RequestException:
            return None
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
            return None
        return r.content, r.headers["Content-Type"]


_TRADUCOES = {
    "Operational": "Pronta",
    "Printing from SD": "Imprimindo do SD",
    "Printing": "Imprimindo",
    "Pausing": "Pausando…",
    "Paused": "Pausada",
    "Resuming": "Retomando…",
    "Cancelling": "Cancelando…",
    "Finishing": "Finalizando…",
    "Starting": "Começando…",
    "Offline": "Desconectada",
    "Connecting": "Conectando…",
    "Error": "Erro",
}


def _traduz(texto):
    for en, pt in _TRADUCOES.items():
        if texto.startswith(en):
            return pt + texto[len(en):]
    return texto
