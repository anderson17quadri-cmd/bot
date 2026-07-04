"""
copy_trade.py  -  Copy Trading: seguir as compras de carteiras "top"
============================================================================
Segue uma lista MANUAL de carteiras (config.COPY_TRADE_WALLETS). Sempre que
uma delas COMPRA um token, o bot pode replicar a compra (um valor pequeno e
fixo), depois de uma verificacao MINIMA de seguranca. E o 4o caminho de
compra dedicado - propositadamente isolado dos outros 3 (normal, bonding
curve, sniper caveira), tal como eles sao isolados entre si.

Como sabemos que uma carteira comprou um token SEM interpretar swaps
complexos:
  Pedimos as ultimas assinaturas da carteira (getSignaturesForAddress) e,
  para cada transacao nova, olhamos para as variacoes de saldo de tokens
  (pre/postTokenBalances). Se o saldo de um token (que nao um estavel)
  AUMENTOU para aquela carteira, ela adquiriu esse token - e o nosso sinal
  de "copia". Nao precisamos de saber o preco nem a DEX; fazemos a nossa
  propria cotacao e compra a seguir.

Este ficheiro tem DUAS responsabilidades bem separadas:
  1. CopyTrader  - monitor em thread de fundo que emite sinais de compra.
  2. Limite diario (gasto_hoje_usd / pode_gastar / registar_gasto) - a
     mesma trava obrigatoria do modo sniper, aplicada aqui tambem, porque
     copiar cegamente carteiras alheias tambem pode sangrar num dia mau.

Tudo fica atras de COPY_TRADE_ATIVO (desligado por defeito).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import config
import rpc

# --------------------------------------------------------------------------
# Parte 1: Limite diario (mesmo padrao do sniper_rapido.py, ficheiro proprio)
# --------------------------------------------------------------------------
FICHEIRO_ESTADO = "copy_trade.json"


def _hoje() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _carregar() -> dict:
    hoje = _hoje()
    vazio = {"data": hoje, "gasto_usd": 0.0, "compras_hoje": 0}
    if not os.path.exists(FICHEIRO_ESTADO):
        return vazio
    try:
        with open(FICHEIRO_ESTADO, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except (json.JSONDecodeError, OSError):
        return vazio
    if dados.get("data") != hoje:
        return vazio  # novo dia -> reinicia
    return {
        "data": dados.get("data", hoje),
        "gasto_usd": float(dados.get("gasto_usd", 0.0)),
        "compras_hoje": int(dados.get("compras_hoje", 0)),
    }


def _guardar(dados: dict) -> None:
    tmp = FICHEIRO_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2)
    os.replace(tmp, FICHEIRO_ESTADO)


def gasto_hoje_usd() -> float:
    return _carregar()["gasto_usd"]


def restante_hoje_usd() -> float:
    return max(0.0, config.COPY_TRADE_LIMITE_DIARIO_USD - gasto_hoje_usd())


def pode_gastar(valor_usd: float) -> bool:
    return (gasto_hoje_usd() + valor_usd) <= config.COPY_TRADE_LIMITE_DIARIO_USD


def registar_gasto(valor_usd: float) -> None:
    """Somar ao gasto de hoje. Chamar SO depois de a compra ter mesmo sido
    efetuada (nunca antes, nunca numa tentativa falhada)."""
    dados = _carregar()
    dados["gasto_usd"] += valor_usd
    dados["compras_hoje"] += 1
    _guardar(dados)


# --------------------------------------------------------------------------
# Parte 2: Monitor das carteiras (thread de fundo)
# --------------------------------------------------------------------------
# Tokens "estaveis"/base que NAO contam como uma compra a copiar (a carteira
# so os usa para pagar). SOL e USDC vem da config; junta-se o USDT.
_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def carteiras_configuradas() -> list[str]:
    """Lista limpa das carteiras a seguir (COPY_TRADE_WALLETS, separadas
    por virgula). Ignora entradas vazias e espacos."""
    crua = config.COPY_TRADE_WALLETS or ""
    return [w.strip() for w in crua.split(",") if w.strip()]


def _mints_estaveis() -> set[str]:
    return {config.MINT_SOL, config.MINT_USDC, _USDT}


def _tokens_comprados(tx: dict, carteira: str) -> list[str]:
    """Dada uma transacao (getTransaction jsonParsed) e uma carteira,
    devolve os mints cujo saldo da carteira AUMENTOU (ou seja, que ela
    adquiriu). Exclui os tokens estaveis/base. Nunca levanta excecao."""
    try:
        meta = tx.get("meta") or {}
        estaveis = _mints_estaveis()

        def saldos(lista) -> dict:
            acc: dict = {}
            for b in lista or []:
                if b.get("owner") != carteira:
                    continue
                mint = b.get("mint")
                if not mint or mint in estaveis:
                    continue
                bruto = (b.get("uiTokenAmount") or {}).get("amount") or "0"
                try:
                    acc[mint] = acc.get(mint, 0.0) + float(bruto)
                except (TypeError, ValueError):
                    continue
            return acc

        antes = saldos(meta.get("preTokenBalances"))
        depois = saldos(meta.get("postTokenBalances"))
        comprados = []
        for mint, dep in depois.items():
            if dep > antes.get(mint, 0.0):  # saldo subiu -> adquiriu
                comprados.append(mint)
        return comprados
    except Exception:
        return []


class CopyTrader:
    """Monitoriza as carteiras seguidas e poe na fila os tokens que elas
    compraram. O main.py drena a fila (buscar_sinais) a cada ciclo, tal
    como faz com os detetores.

    IMPORTANTE: no arranque NAO reproduz o historico - regista qual e a
    assinatura mais recente de cada carteira e so emite compras DAI para a
    frente (senao, ao ligar, copiava tudo o que a carteira ja tinha feito).
    """

    def __init__(self, carteiras: list[str], limitador=None):
        self.carteiras = list(carteiras)
        self._limitador = limitador
        self._ultima_sig: dict[str, str | None] = {}
        self._fila: list[dict] = []
        self._vistos: set[str] = set()  # (carteira|mint) ja emitidos, dedup
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _rpc(self, method: str, params: list):
        """Chamada RPC com o limitador de ritmo (para nao estourar o plano
        gratuito). Devolve None em qualquer falha - o monitor tolera."""
        if self._limitador is not None:
            self._limitador.adquirir(timeout=10)
        try:
            return rpc.rpc_call(method, params, tentativas=2)
        except Exception:
            return None

    def _sig_mais_recente(self, carteira: str) -> str | None:
        assinaturas = self._rpc(
            "getSignaturesForAddress", [carteira, {"limit": 10}]
        )
        if isinstance(assinaturas, list) and assinaturas:
            return assinaturas[0].get("signature")
        return None

    def _loop(self) -> None:
        # Arranque: fixa a "cabeca" de cada carteira sem emitir nada
        for w in self.carteiras:
            self._ultima_sig[w] = self._sig_mais_recente(w)
        print(f"[copy_trade] a seguir {len(self.carteiras)} carteira(s)")

        while not self._parar.is_set():
            for w in self.carteiras:
                if self._parar.is_set():
                    break
                try:
                    self._verificar_carteira(w)
                except Exception as e:
                    print(f"[copy_trade] erro a verificar {w[:8]}...: {e}")
            self._parar.wait(config.COPY_TRADE_INTERVALO_SEGUNDOS)

    def _verificar_carteira(self, carteira: str) -> None:
        assinaturas = self._rpc(
            "getSignaturesForAddress", [carteira, {"limit": 10}]
        )
        if not isinstance(assinaturas, list) or not assinaturas:
            return

        cabeca_antiga = self._ultima_sig.get(carteira)
        # Recolhe as assinaturas NOVAS (mais recentes que a ultima vista).
        # A lista vem da mais recente para a mais antiga.
        novas = []
        for a in assinaturas:
            sig = a.get("signature")
            if sig == cabeca_antiga:
                break
            if a.get("err") is not None:
                continue  # transacao que falhou on-chain - ignora
            novas.append(sig)
        # Atualiza a cabeca para a mais recente (mesmo que nada seja emitido)
        self._ultima_sig[carteira] = assinaturas[0].get("signature")

        if cabeca_antiga is None:
            return  # primeira passagem ja tratada no arranque

        # Processa da mais antiga para a mais recente (ordem cronologica)
        for sig in reversed(novas):
            tx = self._rpc("getTransaction", [
                sig,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ])
            if not tx:
                continue
            for mint in _tokens_comprados(tx, carteira):
                chave = f"{carteira}|{mint}"
                with self._lock:
                    if chave in self._vistos:
                        continue
                    self._vistos.add(chave)
                    self._fila.append({
                        "mint": mint,
                        "carteira": carteira,
                        "assinatura": sig,
                    })

    def buscar_sinais(self) -> list[dict]:
        """Drena e devolve os sinais de compra desde a ultima chamada."""
        with self._lock:
            sinais = list(self._fila)
            self._fila.clear()
        return sinais

    def parar(self) -> None:
        self._parar.set()


# --------------------------------------------------------------------------
# Teste rapido:  python3 copy_trade.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Ativo            : {config.COPY_TRADE_ATIVO}")
    print(f"Carteiras        : {carteiras_configuradas()}")
    print(f"Valor por copia  : ${config.COPY_TRADE_VALOR_USD:.2f}")
    print(f"Limite diario    : ${config.COPY_TRADE_LIMITE_DIARIO_USD:.2f}")
    print(f"Gasto hoje       : ${gasto_hoje_usd():.2f}")
    if carteiras_configuradas():
        print("\nA ouvir compras durante 30s...\n")
        ct = CopyTrader(carteiras_configuradas())
        fim = time.time() + 30
        while time.time() < fim:
            for s in ct.buscar_sinais():
                print(f"  + {s['carteira'][:8]}... comprou {s['mint']}")
            time.sleep(1)
        ct.parar()
        print("\nfim.")
