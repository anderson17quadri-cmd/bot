"""
carteira_tokens.py  -  Listar e enviar tokens SPL da wallet do bot (Solana)
=============================================================================
Duas funcoes para o dashboard:

  listar_tokens()  -> TODOS os tokens SPL que a wallet do bot detem
                      agora (nao so os que o bot comprou). So leitura.

  enviar_token()   -> transfere uma quantidade de um token para outro
                      endereco. ACAO IRREVERSIVEL COM DINHEIRO REAL.

Seguranca do envio (tripla, como as outras acoes de dinheiro real):
  1. config.DRY_RUN            -> em dry-run nunca envia, so simula
  2. CONFIRMO no pedido        -> exigido pela rota do dashboard em modo real
  3. config.PERMITIR_ENVIO_TOKENS -> trava final; sem ela nao ha envio
     on-chain mesmo em modo real. Alem disso, antes de enviar corremos
     simulateTransaction e abortamos se falhar.

O construtor da transacao segue o programa SPL Token (transferChecked) e
cria a conta de token do destino se ainda nao existir (createIdempotent).
NAO foi validado com um envio real em mainnet - por isso a trava.
"""

import base64
import struct

import requests

import config

# solders so e importado aqui (biblioteca nativa); o dashboard aguenta a
# sua ausencia porque so chama este modulo nas acoes, nao ao ver dados.
from solders.pubkey import Pubkey

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYS_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")


def _rpc(metodo: str, params: list):
    """Chamada JSON-RPC a Solana. Levanta em erro (quem chama trata)."""
    r = requests.post(config.SOLANA_RPC_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": metodo, "params": params,
    }, timeout=20)
    r.raise_for_status()
    resposta = r.json()
    if "error" in resposta:
        raise RuntimeError(resposta["error"])
    return resposta["result"]


def _ata(dono: Pubkey, mint: Pubkey) -> Pubkey:
    """Endereco da conta de token (ATA) de um dono para um mint."""
    return Pubkey.find_program_address(
        [bytes(dono), bytes(TOKEN_PROGRAM), bytes(mint)], ATA_PROGRAM
    )[0]


# ==========================================================================
# Listagem (read-only)
# ==========================================================================
def _listar_via_das(endereco: str) -> list | None:
    """Tenta a API DAS da Helius (getAssetsByOwner) - devolve tambem
    simbolo e valor em USD. So funciona se o RPC for Helius; senao None."""
    if "helius" not in config.SOLANA_RPC_URL.lower():
        return None
    try:
        resultado = _rpc("getAssetsByOwner", [{
            "ownerAddress": endereco,
            "page": 1, "limit": 100,
            "displayOptions": {"showFungible": True},
        }])
    except Exception:
        return None

    tokens = []
    for item in resultado.get("items", []):
        if item.get("interface") not in ("FungibleToken", "FungibleAsset"):
            continue
        ti = item.get("token_info", {}) or {}
        dec = ti.get("decimals", 0)
        bruto = ti.get("balance", 0)
        quantidade = bruto / (10 ** dec) if dec else bruto
        if quantidade <= 0:
            continue
        preco = (ti.get("price_info") or {}).get("total_price")
        simbolo = ti.get("symbol") or (item.get("content", {}).get("metadata", {}) or {}).get("symbol") or "?"
        tokens.append({
            "mint": item.get("id"),
            "simbolo": simbolo,
            "quantidade": quantidade,
            "decimais": dec,
            "valor_usd": round(preco, 2) if preco is not None else None,
        })
    return tokens


def _listar_via_rpc(endereco: str) -> list:
    """Fallback universal: getTokenAccountsByOwner. Da mint + quantidade,
    mas nao simbolo nem preco (esses precisam de metadados/Helius)."""
    resultado = _rpc("getTokenAccountsByOwner", [
        endereco, {"programId": str(TOKEN_PROGRAM)}, {"encoding": "jsonParsed"},
    ])
    tokens = []
    for conta in resultado.get("value", []):
        info = conta["account"]["data"]["parsed"]["info"]
        amt = info["tokenAmount"]
        if not amt.get("uiAmount"):
            continue  # salta contas com saldo 0
        tokens.append({
            "mint": info["mint"],
            "simbolo": "?",              # sem metadados aqui
            "quantidade": amt["uiAmount"],
            "decimais": amt["decimals"],
            "valor_usd": None,
        })
    return tokens


def listar_tokens() -> dict:
    """Lista os tokens SPL da wallet do bot. Prefere a Helius (simbolo +
    USD); cai para o RPC generico se nao for Helius. Tolerante a falha."""
    import wallet
    try:
        endereco = wallet.endereco_publico()
    except Exception as e:
        return {"configurada": False, "erro": str(e), "tokens": []}

    try:
        tokens = _listar_via_das(endereco)
        if tokens is None:
            tokens = _listar_via_rpc(endereco)
    except Exception as e:
        return {"configurada": True, "endereco": endereco, "erro": str(e), "tokens": []}

    # Maior valor primeiro (os sem preco vao para o fim)
    tokens.sort(key=lambda t: t.get("valor_usd") or -1, reverse=True)
    return {"configurada": True, "endereco": endereco, "tokens": tokens}


# ==========================================================================
# Envio (acao irreversivel - fortemente trancada)
# ==========================================================================
def enviar_token(mint_str: str, destino_str: str, quantidade_ui: float) -> dict:
    """Envia 'quantidade_ui' (na unidade "humana" do token) do 'mint'
    para o endereco 'destino'.

    Em DRY_RUN: valida tudo mas NAO envia (devolve "simulado").
    Em real: exige config.PERMITIR_ENVIO_TOKENS, corre simulateTransaction
    e so entao envia. Nunca levanta excecao para fora.
    """
    # --- Validacao dos inputs (comum a dry-run e real) ---
    try:
        mint = Pubkey.from_string(mint_str)
        destino = Pubkey.from_string(destino_str)
    except Exception:
        return {"sucesso": False, "mensagem": "Endereço de destino ou mint inválido."}
    if quantidade_ui <= 0:
        return {"sucesso": False, "mensagem": "Quantidade tem de ser maior que zero."}

    try:
        import wallet
        keypair = wallet.obter_keypair()
        dono = keypair.pubkey()
    except Exception as e:
        return {"sucesso": False, "mensagem": f"Wallet indisponível: {e}"}

    # --- Descobrir os decimais e o saldo do token (da conta de origem) ---
    origem_ata = _ata(dono, mint)
    try:
        saldo = _rpc("getTokenAccountBalance", [str(origem_ata)])["value"]
        decimais = int(saldo["decimals"])
        saldo_ui = float(saldo["uiAmount"] or 0)
    except Exception:
        return {"sucesso": False, "mensagem": "A wallet não tem este token (ou conta inexistente)."}

    if quantidade_ui > saldo_ui:
        return {"sucesso": False, "mensagem": f"Saldo insuficiente: tens {saldo_ui}, queres enviar {quantidade_ui}."}

    quantidade_base = int(round(quantidade_ui * (10 ** decimais)))

    # --- DRY-RUN: nunca envia ---
    if config.DRY_RUN:
        return {"sucesso": True, "dry_run": True,
                "mensagem": f"[SIMULADO] Enviaria {quantidade_ui} para {destino_str[:8]}... (nada foi enviado)."}

    # --- REAL: trava final ---
    if not config.PERMITIR_ENVIO_TOKENS:
        return {"sucesso": False, "dry_run": False,
                "mensagem": "Envio real bloqueado (PERMITIR_ENVIO_TOKENS=false no .env). Nada foi enviado."}

    return _enviar_real(keypair, dono, mint, destino, origem_ata, quantidade_base, decimais, quantidade_ui, destino_str)


def _enviar_real(keypair, dono, mint, destino, origem_ata, quantidade_base,
                 decimais, quantidade_ui, destino_str) -> dict:
    """Constroi (createIdempotent ATA do destino + transferChecked),
    simula e envia. NAO validado com envio real em mainnet."""
    try:
        from solders.instruction import Instruction, AccountMeta
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        from solders.hash import Hash

        destino_ata = _ata(destino, mint)

        # 1) Cria a conta de token do destino se nao existir (idempotente:
        #    se ja existir, nao faz nada). Instrucao 1 do ATA program.
        ix_criar = Instruction(
            ATA_PROGRAM, bytes([1]),
            [
                AccountMeta(dono, True, True),        # payer
                AccountMeta(destino_ata, False, True),
                AccountMeta(destino, False, False),   # owner do ATA
                AccountMeta(mint, False, False),
                AccountMeta(SYS_PROGRAM, False, False),
                AccountMeta(TOKEN_PROGRAM, False, False),
            ],
        )

        # 2) transferChecked (instrucao 12): [12] + amount(u64) + decimals(u8)
        dados = bytes([12]) + struct.pack("<Q", quantidade_base) + bytes([decimais])
        ix_transf = Instruction(
            TOKEN_PROGRAM, dados,
            [
                AccountMeta(origem_ata, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(destino_ata, False, True),
                AccountMeta(dono, True, False),       # owner assina
            ],
        )

        bh = _rpc("getLatestBlockhash", [])["value"]["blockhash"]
        msg = MessageV0.try_compile(dono, [ix_criar, ix_transf], [], Hash.from_string(bh))
        tx = VersionedTransaction(msg, [keypair])
        tx_b64 = base64.b64encode(bytes(tx)).decode()

        # Simula antes de enviar
        sim = _rpc("simulateTransaction", [tx_b64, {"encoding": "base64",
                   "sigVerify": False, "replaceRecentBlockhash": True}])
        if sim.get("value", {}).get("err") is not None:
            return {"sucesso": False, "dry_run": False,
                    "mensagem": f"Simulação falhou (não enviado): {sim['value']['err']}"}

        assinatura = _rpc("sendTransaction", [tx_b64, {"encoding": "base64",
                          "skipPreflight": False, "maxRetries": 3}])
        return {"sucesso": True, "dry_run": False, "assinatura": assinatura,
                "mensagem": f"Enviado {quantidade_ui} para {destino_str[:8]}... - tx {assinatura[:12]}..."}

    except Exception as e:
        return {"sucesso": False, "dry_run": False, "mensagem": f"Falha ao enviar: {e}"}
