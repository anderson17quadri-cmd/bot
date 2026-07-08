"""
gerar_wallet_bsc.py  -  Gera uma wallet NOVA e dedicada para a BSC
====================================================================
Cria um par endereco/chave privada Ethereum (0x...) do zero, usando
'eth-account'. NAO le nem escreve o .env - so imprime os valores para
copiares tu mesmo.

Uso:
    python3 gerar_wallet_bsc.py

Depois de correr:
  1) Copia o ENDERECO e a CHAVE PRIVADA para o teu .env
     (WALLET_PRIVATE_KEY_BSC=...).
  2) Envia uma pequena quantidade de BNB para o ENDERECO (so o suficiente
     para gas + os trades pequenos que configurares).
  3) Limpa o terminal (ex: comando "clear") depois de copiares a chave -
     nao a deixes visivel no historico do ecra.

NUNCA reutilizes esta chave nem a wallet principal - cria sempre uma
carteira NOVA, dedicada so a este bot, com um valor pequeno que aceitas
arriscar.
"""

from eth_account import Account


def main() -> None:
    Account.enable_unaudited_hdwallet_features()
    conta = Account.create()

    print("=" * 70)
    print("NOVA WALLET BSC (Ethereum) GERADA")
    print("=" * 70)
    print()
    print(f"Endereco publico : {conta.address}")
    print(f"Chave privada    : {conta.key.hex()}")
    print()
    print("Proximos passos:")
    print("  1) Copia a chave privada para o .env:")
    print(f"     WALLET_PRIVATE_KEY_BSC={conta.key.hex()}")
    print("  2) Envia um pouco de BNB para o endereco acima (gas + trades).")
    print("  3) Limpa o terminal a seguir (comando 'clear') - nao deixes a")
    print("     chave privada visivel no historico do ecra.")
    print()
    print("NUNCA partilhes a chave privada nem a reutilizes noutro lado.")
    print("=" * 70)


if __name__ == "__main__":
    main()
