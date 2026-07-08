"""
wallet_bsc.py  -  Wallet dedicada ao bot na BSC (formato Ethereum)
====================================================================
A BSC usa enderecos e chaves no formato Ethereum (0x...), DIFERENTE da
Solana. Por isso a wallet e SEPARADA: a chave vem de WALLET_PRIVATE_KEY_BSC
no .env (nunca a mesma da Solana - nunca reutilizar chaves entre chains).

Usa 'eth-account' (leve, pure-Python) para assinar - sem o web3.py
completo, que e pesado e pode falhar a compilar no Termux.

    pip install eth-account
"""

import requests
from eth_account import Account

import config


class WalletBscNaoConfiguradaError(Exception):
    """A WALLET_PRIVATE_KEY_BSC nao esta definida no .env."""


_conta_cache = None


def _carregar_conta():
    """Cria (uma vez) o objeto Account a partir da chave privada 0x..."""
    global _conta_cache
    if _conta_cache is not None:
        return _conta_cache

    if not config.WALLET_PRIVATE_KEY_BSC:
        raise WalletBscNaoConfiguradaError(
            "WALLET_PRIVATE_KEY_BSC nao esta definida no .env. Gera uma "
            "wallet Ethereum NOVA e dedicada so a este bot - nunca uses a "
            "tua wallet principal, nem a mesma chave da Solana."
        )
    try:
        _conta_cache = Account.from_key(config.WALLET_PRIVATE_KEY_BSC)
    except Exception as e:
        raise WalletBscNaoConfiguradaError(f"WALLET_PRIVATE_KEY_BSC invalida: {e}") from e
    return _conta_cache


def endereco_publico() -> str:
    """Endereco publico (0x...) da wallet BSC do bot."""
    return _carregar_conta().address


def obter_conta():
    """Objeto Account (usado pelo executor_bsc para assinar)."""
    return _carregar_conta()


def obter_saldo_bnb() -> float:
    """Saldo em BNB da wallet, via eth_getBalance (chamada RPC direta)."""
    endereco = endereco_publico()
    resposta = requests.post(config.BSC_RPC_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
        "params": [endereco, "latest"],
    }, timeout=15)
    resposta.raise_for_status()
    wei = int(resposta.json()["result"], 16)
    return wei / 1e18  # 1 BNB = 1e18 wei


# --------------------------------------------------------------------------
# Teste rapido:  python3 wallet_bsc.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        print("Endereco BSC:", endereco_publico())
        print("Saldo       :", obter_saldo_bnb(), "BNB")
    except WalletBscNaoConfiguradaError as e:
        print("Wallet BSC nao configurada:", e)
