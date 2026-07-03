"""
executor_pumpfun.py  -  Compra DIRETA na bonding curve do pump.fun
====================================================================
!!! O MODULO MAIS EXPERIMENTAL E ARRISCADO DE TODO O PROJETO !!!

Contexto: no pump.fun, um token novo comeca numa "bonding curve" (uma
formula de preco automatica, sem pool tradicional). So DEPOIS de atingir
~$69k de market cap e que "gradua" para um pool a serio (PumpSwap/Raydium),
compravel via Jupiter. O executor.py normal so compra tokens JA graduados.

Este modulo compra ANTES da graduacao, falando diretamente com o programa
on-chain do pump.fun. Porque e tao arriscado:
  - A ESMAGADORA MAIORIA dos tokens na curva NUNCA gradua (vao a zero).
  - E onde estao os maiores multiplicadores, mas tambem quase todas as
    mortes e rug pulls.

Fonte da verdade (validada nesta implementacao):
  - IDL oficial: github.com/pump-fun/pump-public-docs (idl/pump.json)
  - Programa   : 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
  - Instrucao 'buy': discriminator 66063d1201daebea, args
    (amount u64, max_sol_cost u64, track_volume OptionBool), 16 contas.

Matematica do preco (constant-product, estilo Uniswap):
    preco_por_token = virtual_quote_reserves / virtual_token_reserves
    custo(N tokens) = virtual_quote * N / (virtual_token - N)  (+ taxa ~1%)

SEGURANCA - duas travas independentes para o envio real:
  1. config.DRY_RUN                 (a mesma do resto do bot)
  2. config.PUMPFUN_PERMITIR_ENVIO_REAL  (especifica desta curva)
O envio on-chain SO acontece se AMBAS permitirem. Mesmo assim, o
construtor da transacao foi escrito a partir do IDL mas NAO foi validado
com uma compra real em mainnet (isso custa SOL de verdade) - por isso,
antes de enviar, corremos sempre simulateTransaction e abortamos se a
simulacao falhar.
"""

import base64
import struct

import requests

import config

# solders so e importado quando este modulo e usado (biblioteca nativa).
from solders.pubkey import Pubkey

# --- Enderecos oficiais do ecossistema pump.fun (do IDL) ---
PROGRAMA_PUMP = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PROGRAMA_FEE = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYS_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")

# Discriminator da instrucao 'buy' (8 bytes, do IDL)
DISCRIMINATOR_BUY = bytes.fromhex("66063d1201daebea")

# Seed constante de 32 bytes do PDA fee_config (do IDL; vive no PROGRAMA_FEE)
FEE_CONFIG_SEED_CONST = bytes.fromhex(
    "0156e0f693665acf44db1568bf175baa5189cb97f5d2ff3b655d2bb6fd6d18b0"
)

# Desvio (offset) dos campos na conta BondingCurve, depois dos 8 bytes
# do discriminator. Layout confirmado no IDL e lido on-chain.
_OFF = 8


class ErroCurva(Exception):
    """Falha ao ler/interpretar a bonding curve (token ja graduou, etc)."""


# ==========================================================================
# Leitura do estado da curva (read-only, robusto)
# ==========================================================================
def _pda_bonding_curve(mint: Pubkey) -> Pubkey:
    """PDA da conta da curva: seeds ["bonding-curve", mint]."""
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PROGRAMA_PUMP)[0]


def ler_estado_curva(mint_str: str) -> dict:
    """Le e interpreta a conta da bonding curve de um token.

    Devolve:
      {
        "virtual_token_reserves": int,   # unidades minimas (6 decimais)
        "virtual_quote_reserves": int,   # lamports de SOL
        "real_token_reserves": int,
        "real_quote_reserves": int,
        "token_total_supply": int,
        "complete": bool,                # True = ja graduou (nao comprar na curva!)
        "creator": str,                  # pubkey do criador
        "existe": bool,
      }
    Levanta ErroCurva se a conta nao existir (token ja migrado, ou mint
    invalido). Nunca deixa uma excecao "crua" escapar.
    """
    try:
        mint = Pubkey.from_string(mint_str)
    except Exception as e:
        raise ErroCurva(f"mint invalido: {e}") from e

    pda = _pda_bonding_curve(mint)
    try:
        resposta = requests.post(config.SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [str(pda), {"encoding": "base64"}],
        }, timeout=15)
        resposta.raise_for_status()
        valor = resposta.json().get("result", {}).get("value")
    except Exception as e:
        raise ErroCurva(f"falha de RPC ao ler a curva: {e}") from e

    if not valor:
        raise ErroCurva("sem conta de curva (token ja graduou/migrou ou nao e pump.fun)")

    dados = base64.b64decode(valor["data"][0])
    if len(dados) < _OFF + 41:
        raise ErroCurva("conta da curva com tamanho inesperado")

    vtok, vquote, rtok, rquote, supply = struct.unpack_from("<QQQQQ", dados, _OFF)
    complete = bool(dados[_OFF + 40])
    # creator vive logo a seguir ao bool 'complete' (pubkey = 32 bytes)
    creator = Pubkey.from_bytes(dados[_OFF + 41:_OFF + 73])

    return {
        "virtual_token_reserves": vtok,
        "virtual_quote_reserves": vquote,
        "real_token_reserves": rtok,
        "real_quote_reserves": rquote,
        "token_total_supply": supply,
        "complete": complete,
        "creator": str(creator),
        "existe": True,
    }


def preco_por_token_sol(estado: dict) -> float:
    """Preco atual de 1 token (unidade inteira, 6 decimais) em SOL."""
    vtok = estado["virtual_token_reserves"]
    vquote = estado["virtual_quote_reserves"]
    if vtok <= 0:
        return 0.0
    # (lamports/1e9 SOL) por (unidades/1e6 tokens)
    return (vquote / 1e9) / (vtok / 1e6)


def calcular_compra(estado: dict, valor_sol: float) -> dict:
    """Dado o estado da curva e quanto SOL queres gastar, calcula quantos
    tokens recebes (constant-product) e o preco medio efetivo.

    Formula: mantendo k = vtok * vquote constante, ao adicionar
    'valor_sol' aos quote reserves, os token reserves descem para
    k / (vquote + gasto), e a diferenca sao os tokens que recebes.

    Nao inclui a taxa (~1%) do pump.fun - essa e somada por cima ao
    custo real; para a simulacao de quantos tokens recebes, o efeito e
    pequeno e conservador (recebes ligeiramente menos).
    """
    vtok = estado["virtual_token_reserves"]
    vquote = estado["virtual_quote_reserves"]
    gasto_lamports = int(valor_sol * 1e9)

    if vtok <= 0 or vquote <= 0 or gasto_lamports <= 0:
        return {"tokens_recebidos": 0, "preco_medio_sol": 0.0}

    k = vtok * vquote
    novo_vquote = vquote + gasto_lamports
    novo_vtok = k // novo_vquote
    tokens_recebidos = vtok - novo_vtok  # unidades minimas (6 decimais)

    preco_medio = (valor_sol / (tokens_recebidos / 1e6)) if tokens_recebidos > 0 else 0.0
    return {
        "tokens_recebidos": tokens_recebidos,
        "preco_medio_sol": preco_medio,
    }


# ==========================================================================
# Derivacao de TODAS as contas da instrucao 'buy' (parte fragil)
# ==========================================================================
def _ata(dono: Pubkey, mint: Pubkey) -> Pubkey:
    """Associated Token Account de um dono para um mint."""
    return Pubkey.find_program_address(
        [bytes(dono), bytes(TOKEN_PROGRAM), bytes(mint)], ATA_PROGRAM
    )[0]


def _derivar_contas_buy(mint: Pubkey, comprador: Pubkey, estado: dict) -> list:
    """Deriva, pela ORDEM EXATA do IDL, as 16 contas da instrucao 'buy'.

    Uma so conta errada aqui = a transacao falha on-chain. Por isso cada
    PDA segue exatamente as seeds declaradas no idl/pump.json.
    """
    fee_recipient = _ler_fee_recipient()  # do Global account
    bonding_curve = _pda_bonding_curve(mint)
    creator = Pubkey.from_string(estado["creator"])

    global_pda = Pubkey.find_program_address([b"global"], PROGRAMA_PUMP)[0]
    creator_vault = Pubkey.find_program_address([b"creator-vault", bytes(creator)], PROGRAMA_PUMP)[0]
    event_authority = Pubkey.find_program_address([b"__event_authority"], PROGRAMA_PUMP)[0]
    global_vol = Pubkey.find_program_address([b"global_volume_accumulator"], PROGRAMA_PUMP)[0]
    user_vol = Pubkey.find_program_address([b"user_volume_accumulator", bytes(comprador)], PROGRAMA_PUMP)[0]
    fee_config = Pubkey.find_program_address([b"fee_config", FEE_CONFIG_SEED_CONST], PROGRAMA_FEE)[0]

    # (pubkey, is_signer, is_writable) - ordem e flags exatamente do IDL
    return [
        (global_pda, False, False),
        (fee_recipient, False, True),
        (mint, False, False),
        (bonding_curve, False, True),
        (_ata(bonding_curve, mint), False, True),   # associated_bonding_curve
        (_ata(comprador, mint), False, True),        # associated_user
        (comprador, True, True),                     # user (signer)
        (SYS_PROGRAM, False, False),
        (TOKEN_PROGRAM, False, False),
        (creator_vault, False, True),
        (event_authority, False, False),
        (PROGRAMA_PUMP, False, False),
        (global_vol, False, False),
        (user_vol, False, True),
        (fee_config, False, False),
        (PROGRAMA_FEE, False, False),
    ]


_fee_recipient_cache = None


def _ler_fee_recipient() -> Pubkey:
    """Le o fee_recipient da conta Global do programa (campo apos
    initialized+authority). Fica em cache - nao muda entre trades."""
    global _fee_recipient_cache
    if _fee_recipient_cache is not None:
        return _fee_recipient_cache

    global_pda = Pubkey.find_program_address([b"global"], PROGRAMA_PUMP)[0]
    resposta = requests.post(config.SOLANA_RPC_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [str(global_pda), {"encoding": "base64"}],
    }, timeout=15)
    resposta.raise_for_status()
    dados = base64.b64decode(resposta.json()["result"]["value"]["data"][0])
    # layout Global: disc(8) + initialized(1 bool) + authority(32) + fee_recipient(32)
    inicio = 8 + 1 + 32
    _fee_recipient_cache = Pubkey.from_bytes(dados[inicio:inicio + 32])
    return _fee_recipient_cache


# ==========================================================================
# Compra na curva (dry-run simula; real constroi + simula + [talvez] envia)
# ==========================================================================
def comprar_na_curva(mint: str, simbolo: str, valor_usd: float, preco_sol_usd: float) -> dict:
    """Compra 'valor_usd' de um token AINDA na bonding curve.

    Fluxo:
      1. Le o estado da curva; se ja graduou ('complete'), recusa.
      2. Calcula tokens a receber pela formula constant-product.
      3. DRY_RUN -> so simula (regista na carteira/posicoes virtuais).
      4. REAL     -> constroi a transacao, corre simulateTransaction e SO
                     envia se PUMPFUN_PERMITIR_ENVIO_REAL for true E a
                     simulacao passar.

    Devolve um dict com o resultado (mesma forma do executor.py).
    Nunca levanta excecao para fora - falhas viram {"sucesso": False,...}.
    """
    # Limite de trade especifico da curva (mais baixo que o normal)
    teto = min(valor_usd, config.PUMPFUN_MAX_TRADE_USD)
    if teto <= 0:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "Valor de compra invalido."}

    try:
        estado = ler_estado_curva(mint)
    except ErroCurva as e:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": f"[curva] {e}"}

    if estado["complete"]:
        return {"sucesso": False, "dry_run": config.DRY_RUN,
                "mensagem": "Token ja graduou - usa a compra normal (Jupiter), nao a curva."}

    valor_sol = teto / preco_sol_usd if preco_sol_usd > 0 else 0
    calc = calcular_compra(estado, valor_sol)
    tokens = calc["tokens_recebidos"]
    if tokens <= 0:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "Curva nao devolve tokens para este valor."}

    preco_unit_sol = calc["preco_medio_sol"]
    preco_unit_usd = preco_unit_sol * preco_sol_usd

    # ---------------- DRY-RUN: so simula ----------------
    if config.DRY_RUN:
        import carteira
        import posicoes
        if not carteira.registar_compra(simbolo, teto, mint=mint,
                                        preco_unitario_usd=preco_unit_usd,
                                        quantidade_tokens=tokens):
            return {"sucesso": False, "dry_run": True,
                    "mensagem": f"[SIMULADO] Saldo virtual insuficiente para {simbolo}"}
        posicoes.abrir_posicao(
            mint=mint, simbolo=simbolo, valor_investido_usd=teto,
            preco_compra_usd=preco_unit_usd, quantidade_tokens=tokens, dry_run=True,
        )
        # Marca a posicao como sendo de bonding curve (tag no dashboard)
        posicoes.atualizar_posicao(mint, origem="bonding_curve")
        return {"sucesso": True, "dry_run": True, "origem": "bonding_curve",
                "quantidade_tokens": tokens,
                "mensagem": f"[SIMULADO][CURVA] Comprado ${teto:.2f} de {simbolo} (~{tokens/1e6:,.0f} tokens)"}

    # ---------------- REAL: construir + simular + [talvez] enviar ----------------
    return _comprar_real(mint, simbolo, teto, valor_sol, tokens, preco_unit_usd, estado)


def _comprar_real(mint_str, simbolo, valor_usd, valor_sol, tokens_esperados,
                  preco_unit_usd, estado) -> dict:
    """Caminho REAL: constroi a transacao 'buy' a partir do IDL, corre
    simulateTransaction e so envia se a trava PUMPFUN_PERMITIR_ENVIO_REAL
    permitir E a simulacao passar. Documentado como NAO validado com uma
    compra real em mainnet."""
    try:
        import wallet
        from solders.instruction import Instruction, AccountMeta
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        from solders.hash import Hash

        keypair = wallet.obter_keypair()
        comprador = keypair.pubkey()
        mint = Pubkey.from_string(mint_str)

        # max_sol_cost com folga de slippage (protege de derrapagem de preco)
        folga = 1 + (config.SLIPPAGE_BPS / 10_000)
        max_sol_cost = int(valor_sol * 1e9 * folga)

        # Dados da instrucao: discriminator + amount(u64) + max_sol_cost(u64)
        # + track_volume (OptionBool: 0 = None). amount = tokens esperados.
        dados = (DISCRIMINATOR_BUY
                 + struct.pack("<Q", int(tokens_esperados))
                 + struct.pack("<Q", max_sol_cost)
                 + bytes([0]))

        contas = _derivar_contas_buy(mint, comprador, estado)
        metas = [AccountMeta(pubkey=pk, is_signer=s, is_writable=w) for pk, s, w in contas]
        instrucao = Instruction(PROGRAMA_PUMP, dados, metas)

        # Blockhash recente para a mensagem
        bh = requests.post(config.SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [],
        }, timeout=15).json()["result"]["value"]["blockhash"]

        msg = MessageV0.try_compile(comprador, [instrucao], [], Hash.from_string(bh))
        tx = VersionedTransaction(msg, [keypair])
        tx_b64 = base64.b64encode(bytes(tx)).decode()

        # --- simulateTransaction: valida antes de arriscar SOL ---
        sim = requests.post(config.SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [tx_b64, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}],
        }, timeout=20).json()
        erro_sim = sim.get("result", {}).get("value", {}).get("err")
        if erro_sim is not None:
            return {"sucesso": False, "dry_run": False, "origem": "bonding_curve",
                    "mensagem": f"[CURVA] simulacao falhou (nao enviado): {erro_sim}"}

        # --- Trava final: so envia se explicitamente permitido ---
        if not config.PUMPFUN_PERMITIR_ENVIO_REAL:
            return {"sucesso": False, "dry_run": False, "origem": "bonding_curve",
                    "mensagem": ("[CURVA] simulacao OK, mas envio real bloqueado "
                                 "(PUMPFUN_PERMITIR_ENVIO_REAL=false). Nada foi gasto.")}

        envio = requests.post(config.SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        }, timeout=30).json()
        if "error" in envio:
            return {"sucesso": False, "dry_run": False, "origem": "bonding_curve",
                    "mensagem": f"[CURVA] RPC recusou a transacao: {envio['error']}"}

        assinatura = envio["result"]
        import posicoes
        posicoes.abrir_posicao(mint=mint_str, simbolo=simbolo, valor_investido_usd=valor_usd,
                               preco_compra_usd=preco_unit_usd, quantidade_tokens=tokens_esperados,
                               dry_run=False)
        posicoes.atualizar_posicao(mint_str, origem="bonding_curve")
        return {"sucesso": True, "dry_run": False, "origem": "bonding_curve",
                "assinatura": assinatura,
                "mensagem": f"[CURVA] Comprado ${valor_usd:.2f} de {simbolo} - tx {assinatura[:12]}..."}

    except Exception as e:
        return {"sucesso": False, "dry_run": False, "origem": "bonding_curve",
                "mensagem": f"[CURVA] falha ao construir/enviar: {e}"}


# --------------------------------------------------------------------------
# Teste rapido:  python3 executor_pumpfun.py <mint>
# Le a curva de um token e mostra preco + simulacao de compra (nunca compra).
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python3 executor_pumpfun.py <mint de um token na curva>")
        raise SystemExit(1)
    try:
        est = ler_estado_curva(sys.argv[1])
    except ErroCurva as e:
        print("Erro:", e)
        raise SystemExit(1)
    print("complete (graduou?):", est["complete"])
    print(f"preco por token   : {preco_por_token_sol(est):.3e} SOL")
    sim = calcular_compra(est, 0.01)  # simular gastar 0.01 SOL
    print(f"0.01 SOL compraria: {sim['tokens_recebidos']/1e6:,.0f} tokens "
          f"@ {sim['preco_medio_sol']:.3e} SOL/token")
