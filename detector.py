"""
detector.py
===========
Deteta pools/pares NOVOS na Solana.

Estrategia da Fase 1: PERGUNTAR (polling) a uma API gratuita que ja lista os
pools mais recentes -> a GeckoTerminal (endpoint "new_pools").

Porque polling e nao WebSocket em tempo real?
 - Em "modo alerta" a latencia nao interessa (nao estamos a fazer trades).
 - E fiavel e simples, e o RPC publico nem sequer aguenta WebSocket serio.
 - A interface aqui (classe DetectorPools) esta pronta para, na Fase 2 com
   Helius, trocar o "motor" por WebSocket sem mexer no resto do bot.

Nota: a API devolve pools de varios DEX (raydium, pumpswap, orca, meteora...).
E onde os tokens novos aparecem hoje em dia, por isso incluimos todos e
mostramos de qual DEX veio cada um.
"""

from datetime import datetime, timezone

import requests

import config
import html
from conjunto_limitado import ConjuntoLimitado

# Base da GeckoTerminal; a rede (solana/bsc) e escolhida por chamada.
# Documentacao: https://www.geckoterminal.com/dex-api
_BASE_GECKO = "https://api.geckoterminal.com/api/v2/networks"

# A GeckoTerminal recomenda enviar este cabecalho para "fixar" a versao da API.
CABECALHOS = {"Accept": "application/json;version=20230302"}


def _sem_prefixo_rede(token_id: str, rede: str) -> str:
    """A API identifica tokens como '<rede>_<endereco>'. Aqui tiramos o
    prefixo da rede certa.

    Ex (solana): 'solana_HTN638...' -> 'HTN638...'
    Ex (bsc)   : 'bsc_0x95ee...'    -> '0x95ee...'
    """
    prefixo = config.CHAINS[rede]["prefixo"]
    if token_id.startswith(prefixo):
        return token_id[len(prefixo):]
    return token_id


def _idade_minutos(criado_em_iso: str) -> float:
    """Calcula ha quantos minutos o pool foi criado (a partir do timestamp ISO).

    Devolve -1 se nao conseguir interpretar a data.
    """
    if not criado_em_iso:
        return -1.0
    try:
        # A data vem como "2026-07-02T05:26:01Z"; trocamos o Z por +00:00
        criado = datetime.fromisoformat(criado_em_iso.replace("Z", "+00:00"))
        agora = datetime.now(timezone.utc)
        return (agora - criado).total_seconds() / 60.0
    except ValueError:
        return -1.0


def _para_numero(valor) -> float | None:
    """Converte com seguranca para float (a API manda numeros como texto)."""
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _extrair_atividade_recente(attrs: dict) -> dict:
    """Traders unicos + volume da janela mais recente disponivel dos
    campos 'transactions'/'volume_usd' da GeckoTerminal (confirmado nos
    dados reais da API: cada campo tem m5/m15/m30/h1/h6/h24; para pools
    recem-criados costumam vir todos iguais, porque toda a atividade
    aconteceu dentro dos ultimos minutos).

    Prefere a janela m5 (mais recente); cai para m15 SO se m5 vier
    vazia/ausente. Devolve None nos campos que a API nao trouxe - NUNCA
    inventa um 0, para o filtro (main.py) conseguir distinguir "sem
    dados" de "zero real" (um pool com 0 compradores em m5 e diferente
    de um pool sem essa janela reportada)."""
    transacoes = attrs.get("transactions") or {}
    volumes = attrs.get("volume_usd") or {}

    janela_tx = transacoes.get("m5") or transacoes.get("m15") or {}
    volume = _para_numero(volumes.get("m5"))
    if volume is None:
        volume = _para_numero(volumes.get("m15"))

    return {
        "compradores_unicos": janela_tx.get("buyers"),
        "vendedores_unicos": janela_tx.get("sellers"),
        "transacoes_compra": janela_tx.get("buys"),
        "transacoes_venda": janela_tx.get("sells"),
        "volume_usd_recente": volume,
    }


def _interpretar_pool(pool: dict, rede: str) -> dict | None:
    """Transforma o JSON cru de UM pool no nosso formato simples.

    'rede' e "solana" ou "bsc" - decide o prefixo dos ids e quais os
    tokens-base (o "outro lado" do par). Devolve None se faltar
    informacao essencial (ex: sem token).
    """
    attrs = pool.get("attributes", {})
    rel = pool.get("relationships", {})

    # Enderecos dos dois lados do par (mint na Solana, contrato 0x na BSC)
    base_id = rel.get("base_token", {}).get("data", {}).get("id", "")
    quote_id = rel.get("quote_token", {}).get("data", {}).get("id", "")
    base_mint = _sem_prefixo_rede(base_id, rede)
    quote_mint = _sem_prefixo_rede(quote_id, rede)

    dex = rel.get("dex", {}).get("data", {}).get("id", "?")

    # O "name" costuma ser "SIMBOLO_BASE / SIMBOLO_QUOTE" (ex: "favier / SOL")
    nome_par = html.unescape(attrs.get("name", "?"))
    partes = [p.strip() for p in nome_par.split("/")]
    simbolo_base = partes[0] if len(partes) >= 1 else "?"
    simbolo_quote = partes[1] if len(partes) >= 2 else "?"

    # Descobrir qual lado e o TOKEN NOVO. Comparamos com os tokens-base
    # DA REDE (em minusculas na BSC, pois os 0x nao sao case-sensitive).
    bases = config.CHAINS[rede]["bases"]
    base_norm = base_mint.lower() if rede == "bsc" else base_mint
    quote_norm = quote_mint.lower() if rede == "bsc" else quote_mint
    base_e_base = base_norm in bases
    quote_e_base = quote_norm in bases

    if base_e_base and not quote_e_base:
        token_mint, token_simbolo = quote_mint, simbolo_quote
        contra_mint, contra_simbolo = base_mint, simbolo_base
    elif quote_e_base and not base_e_base:
        token_mint, token_simbolo = base_mint, simbolo_base
        contra_mint, contra_simbolo = quote_mint, simbolo_quote
    else:
        # Nenhum lado e uma base conhecida (pool TOKEN/TOKEN) OU os DOIS
        # sao (ex: pool SOL/USDC) - nao ha um lado claramente "o token
        # novo". Adivinhar arrisca analisar/comprar o lado errado; mais
        # seguro descartar este pool do que inventar (filosofia do bot:
        # nunca fingir que sabemos o que nao sabemos).
        return None

    # Sem endereco do token nao ha nada a analisar
    if not token_mint:
        return None

    return {
        "chain": rede,                   # "solana" ou "bsc" (usado a jusante)
        "pool_address": attrs.get("address", "?"),
        "dex": dex,
        "nome_par": nome_par,
        "token_mint": token_mint,
        "token_simbolo": token_simbolo,
        "contra_mint": contra_mint,      # o outro lado (SOL/BNB/estavel)
        "contra_simbolo": contra_simbolo,
        "liquidez_usd": _para_numero(attrs.get("reserve_in_usd")) or 0.0,
        "fdv_usd": _para_numero(attrs.get("fdv_usd")),
        "market_cap_usd": _para_numero(attrs.get("market_cap_usd")),
        "criado_em": attrs.get("pool_created_at", ""),
        "idade_minutos": round(_idade_minutos(attrs.get("pool_created_at", "")), 1),
        **_extrair_atividade_recente(attrs),
    }


def buscar_pools_crus(rede: str = "solana", pagina: int = 1) -> list[dict]:
    """Vai a API buscar a lista de pools recentes DE UMA REDE e devolve-a
    ja interpretada.

    NAO faz filtragem de "ja vistos" - isso e a classe DetectorPools que trata.
    Levanta requests.RequestException se a rede falhar (quem chama decide).
    """
    gecko = config.CHAINS[rede]["gecko"]
    resposta = requests.get(
        f"{_BASE_GECKO}/{gecko}/new_pools",
        headers=CABECALHOS,
        params={"page": pagina},
        timeout=20,
    )
    resposta.raise_for_status()
    dados = resposta.json().get("data", [])

    pools = []
    for pool in dados:
        interpretado = _interpretar_pool(pool, rede)
        if interpretado is not None:
            pools.append(interpretado)
    return pools


class DetectorPools:
    """Guarda quais os pools ja vistos e devolve apenas os NOVOS a cada chamada.

    Uso tipico (em main.py):
        det = DetectorPools()
        while True:
            for pool in det.buscar_novos():
                analisar(pool)
            time.sleep(config.POLL_INTERVAL_SEGUNDOS)
    """

    def __init__(self, rede: str = "solana", emitir_no_arranque: int = 3):
        # Que rede este detector monitoriza ("solana" ou "bsc")
        self.rede = rede
        # Enderecos de pool ja processados (para nao repetir alertas).
        # Capacidade limitada: em 24/7 um set() normal cresceria para
        # sempre (fuga de memoria lenta) - ver conjunto_limitado.py.
        self._vistos = ConjuntoLimitado(capacidade=5000)
        # Na primeira vez, quantos pools recentes emitir logo (para veres
        # output imediato no arranque). Depois disso, so emite os genuinamente novos.
        self._emitir_no_arranque = emitir_no_arranque
        self._primeira_vez = True

    def buscar_novos(self) -> list[dict]:
        """Devolve a lista de pools desta rede que ainda nao tinhamos visto.

        Se houver falha de rede, devolve lista vazia (nao rebenta o ciclo).
        """
        try:
            pools = buscar_pools_crus(self.rede)
        except requests.RequestException as e:
            print(f"[detector:{self.rede}] aviso: falha ao buscar pools ({e})")
            return []

        novos = []
        for pool in pools:
            endereco = pool["pool_address"]
            if endereco in self._vistos:
                continue
            self._vistos.add(endereco)
            novos.append(pool)

        # No arranque, para nao despejar 20 pools antigos de uma vez,
        # devolvemos so os N mais recentes (a API ja vem por ordem de recencia).
        if self._primeira_vez:
            self._primeira_vez = False
            return novos[: self._emitir_no_arranque]

        return novos


# --------------------------------------------------------------------------
# Teste rapido:  python detector.py
# Mostra os pools mais recentes que a API devolve agora.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    rede = sys.argv[1] if len(sys.argv) > 1 else "solana"
    print(f"A procurar pools recentes na rede '{rede}' (GeckoTerminal)...\n")
    try:
        pools = buscar_pools_crus(rede)
    except requests.RequestException as e:
        print(f"Falha de rede: {e}")
        raise SystemExit(1)

    print(f"Encontrados {len(pools)} pools. A mostrar os 8 mais recentes:\n")
    for p in pools[:8]:
        print(f"- [{p['chain']}/{p['dex']}] {p['nome_par']}")
        print(f"    token : {p['token_simbolo']}  ({p['token_mint']})")
        print(f"    liquidez: ${p['liquidez_usd']:,.0f}  | idade: {p['idade_minutos']} min")
        print()
