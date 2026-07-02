"""Gera uma wallet Solana nova. Corre UMA VEZ, guarda a chave em
seguranca, depois apaga este ficheiro."""

from solders.keypair import Keypair
import base58

kp = Keypair()
print("=" * 60)
print("WALLET NOVA GERADA")
print("=" * 60)
print(f"Endereco publico (podes partilhar): {kp.pubkey()}")
print()
print(f"Chave privada (base58) - NUNCA PARTILHES ISTO:")
print(base58.b58encode(bytes(kp)).decode())
print("=" * 60)

