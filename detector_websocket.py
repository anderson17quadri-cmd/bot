"""
detector_websocket.py  -  Deteccao INSTANTANEA de tokens pump.fun (Helius)
============================================================================
Alternativa ao detector.py (polling da GeckoTerminal). Em vez de perguntar
"ha tokens novos?" de X em X segundos, abre uma ligacao WebSocket ao RPC
(Helius) e SUBSCREVE aos logs do programa do pump.fun (logsSubscribe). O
RPC avisa-nos no instante em que um token e criado - sem atraso de polling.

Como extraimos os dados do token SEM uma chamada extra a rede:
  A instrucao 'create' do pump.fun emite um evento Anchor (CreateEvent) que
  aparece nos logs numa linha "Program data: <base64>". Esse evento ja
  contem o mint, o simbolo, o endereco da bonding curve e as reservas
  virtuais iniciais. Decodificamos isso localmente (Borsh) e passamos o
  token ao MESMO pipeline de analise do detector.py (nao duplicamos nada).

Isto e EXPERIMENTAL e so-Solana/pump.fun. Fica atras de METODO_DETECCAO=
websocket no .env - por defeito o bot continua no polling, que e fiavel.

Interface: a classe DetectorWebsocket tem .rede e .buscar_novos(), iguais
ao DetectorPools, para o main.py tratar os dois detetores da mesma forma.
"""

import base64
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone

import config

# Programa do pump.fun e discriminator (8 bytes) do evento de criacao,
# retirados do IDL oficial (idl/pump.json).
PROGRAMA_PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DISCRIMINATOR_CREATE_EVENT = bytes.fromhex("1b72a94ddeeb6376")

# base58 para converter as pubkeys (32 bytes) do evento em enderecos
_ALFA58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(dados: bytes) -> str:
    numero = int.from_bytes(dados, "big")
    texto = ""
    while numero > 0:
        numero, resto = divmod(numero, 58)
        texto = _ALFA58[resto] + texto
    for b in dados:
        if b == 0:
            texto = "1" + texto
        else:
            break
    return texto


def _url_websocket() -> str:
    """URL do WebSocket. Usa SOLANA_WS_URL se definido; senao deriva do
    SOLANA_RPC_URL trocando http(s)->ws(s)."""
    if config.SOLANA_WS_URL:
        return config.SOLANA_WS_URL
    url = config.SOLANA_RPC_URL
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _ler_string(dados: bytes, offset: int) -> tuple[str, int]:
    """Le uma string Borsh (u32 tamanho + bytes utf-8). Devolve (texto, novo_offset)."""
    (tamanho,) = struct.unpack_from("<I", dados, offset)
    offset += 4
    texto = dados[offset:offset + tamanho].decode("utf-8", errors="replace")
    return texto, offset + tamanho


def _descodificar_create_event(dados: bytes) -> dict | None:
    """Descodifica o CreateEvent (ja sem os 8 bytes do discriminator).
    Extrai so o que precisamos. Devolve None se algo nao bater certo."""
    try:
        off = 0
        name, off = _ler_string(dados, off)
        symbol, off = _ler_string(dados, off)
        uri, off = _ler_string(dados, off)
        mint = _b58(dados[off:off + 32]); off += 32
        bonding_curve = _b58(dados[off:off + 32]); off += 32
        off += 32  # user (nao precisamos)
        creator = _b58(dados[off:off + 32]); off += 32
        off += 8   # timestamp i64
        (virtual_token_reserves,) = struct.unpack_from("<Q", dados, off); off += 8
        (virtual_sol_reserves,) = struct.unpack_from("<Q", dados, off); off += 8
        return {
            "name": name, "symbol": symbol, "mint": mint,
            "bonding_curve": bonding_curve, "creator": creator,
            "virtual_token_reserves": virtual_token_reserves,
            "virtual_sol_reserves": virtual_sol_reserves,
        }
    except (struct.error, IndexError, UnicodeDecodeError):
        return None


def _extrair_create_dos_logs(logs: list[str]) -> dict | None:
    """Procura o CreateEvent nas linhas de log de uma transacao. As linhas
    "Program data: <base64>" contem eventos Anchor; o que comeca com o
    discriminator do CreateEvent e o que nos interessa."""
    for linha in logs:
        if not linha.startswith("Program data: "):
            continue
        try:
            crua = base64.b64decode(linha[len("Program data: "):])
        except Exception:
            continue
        if crua[:8] == DISCRIMINATOR_CREATE_EVENT:
            return _descodificar_create_event(crua[8:])
    return None


class DetectorWebsocket:
    """Deteta tokens pump.fun novos em tempo real via logsSubscribe.

    Corre a ligacao WebSocket numa thread de fundo, que vai enchendo uma
    fila interna. O main.py chama buscar_novos() a cada ciclo para drenar
    o que chegou - a mesma interface do DetectorPools.
    """

    def __init__(self, rede: str = "solana", emitir_no_arranque: int = 0):
        self.rede = rede  # sempre "solana" - o pump.fun so existe na Solana
        self._fila: deque = deque()
        self._vistos: set[str] = set()  # mints ja emitidos (dedup)
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """Liga, subscreve e recebe. Reconecta com backoff se cair."""
        from websockets.sync.client import connect

        espera_reconexao = 1.0
        while not self._parar.is_set():
            try:
                with connect(_url_websocket(), open_timeout=15) as ws:
                    # Subscreve aos logs que mencionam o programa do pump.fun.
                    # commitment "processed" = o mais rapido a avisar (menos
                    # garantias de finalidade, mas para DETETAR chega - a
                    # compra em si e que usa um commitment mais seguro).
                    ws.send(
                        '{"jsonrpc":"2.0","id":1,"method":"logsSubscribe",'
                        '"params":[{"mentions":["' + PROGRAMA_PUMP + '"]},'
                        '{"commitment":"processed"}]}'
                    )
                    espera_reconexao = 1.0  # ligou -> reset do backoff
                    print(f"[detector_ws] ligado ({_url_websocket()[:40]}...), a ouvir criacoes pump.fun")

                    for mensagem in ws:  # bloqueia a receber notificacoes
                        if self._parar.is_set():
                            break
                        self._tratar_mensagem(mensagem)
            except Exception as e:
                if self._parar.is_set():
                    break
                print(f"[detector_ws] ligacao caiu ({e}); a reconectar em {espera_reconexao:.0f}s")
                time.sleep(espera_reconexao)
                espera_reconexao = min(espera_reconexao * 2, 30)  # backoff ate 30s

    def _tratar_mensagem(self, mensagem: str) -> None:
        """Interpreta uma notificacao de log e, se for uma criacao de
        token, poe o token na fila no formato do detector.py."""
        import json
        try:
            dados = json.loads(mensagem)
        except json.JSONDecodeError:
            return
        valor = dados.get("params", {}).get("result", {}).get("value", {})
        logs = valor.get("logs")
        if not logs:
            return

        # So nos interessam transacoes que criaram um token
        if not any("Instruction: Create" in l for l in logs):
            return
        evento = _extrair_create_dos_logs(logs)
        if not evento or not evento["mint"]:
            return

        mint = evento["mint"]
        with self._lock:
            if mint in self._vistos:
                return
            self._vistos.add(mint)
            self._fila.append(self._para_formato_pool(evento))

    def _para_formato_pool(self, evento: dict) -> dict:
        """Converte o CreateEvent no MESMO dicionario que o detector.py
        produz, para o pipeline (analyzer, IA, etc.) nao notar diferenca.

        NOTA: a liquidez REAL de um token acabado de nascer e ~0 (a curva
        arranca so com reservas VIRTUAIS). Por isso liquidez_usd fica 0 e
        o analyzer/modos decidem a partir dai - o modo bonding curve, por
        exemplo, rele a curva on-chain de qualquer forma antes de comprar.
        """
        return {
            "chain": "solana",
            "pool_address": evento["bonding_curve"],
            "dex": "pump-fun",
            "nome_par": f"{evento['symbol']} / SOL",
            "token_mint": evento["mint"],
            "token_simbolo": evento["symbol"] or "?",
            "contra_mint": config.MINT_SOL,
            "contra_simbolo": "SOL",
            "liquidez_usd": 0.0,   # recem-nascido: liquidez real ~0 (ver nota)
            "fdv_usd": None,
            "market_cap_usd": None,
            "criado_em": datetime.now(timezone.utc).isoformat(),
            "idade_minutos": 0.0,  # acabou de nascer, por definicao
            "origem_deteccao": "websocket",
        }

    def buscar_novos(self) -> list[dict]:
        """Drena e devolve os tokens detetados desde a ultima chamada."""
        with self._lock:
            novos = list(self._fila)
            self._fila.clear()
        return novos

    def parar(self) -> None:
        self._parar.set()


# --------------------------------------------------------------------------
# Teste rapido:  python3 detector_websocket.py
# Ouve durante ~30s e imprime os tokens pump.fun criados nesse intervalo.
# (Precisa de METODO_DETECCAO=websocket nem nada - so de um RPC com WS,
#  ex: SOLANA_RPC_URL a apontar para o Helius no .env.)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"A ligar ao WebSocket: {_url_websocket()}")
    det = DetectorWebsocket()
    print("A ouvir criacoes de tokens pump.fun durante 30s...\n")
    fim = time.time() + 30
    while time.time() < fim:
        for p in det.buscar_novos():
            print(f"  + {p['token_simbolo']:<12} {p['token_mint']}")
        time.sleep(1)
    det.parar()
    print("\nfim.")
