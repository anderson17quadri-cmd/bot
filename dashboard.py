"""
dashboard.py  -  Dashboard web local do bot (so LEITURA)
==========================================================
Um pequeno servidor Flask que le os ficheiros JSON gerados pelo bot
(carteira.json e posicoes.json) e mostra tudo numa pagina web com
graficos, acessivel no browser do telemovel.

IMPORTANTE:
- Este ficheiro NUNCA escreve nos JSON. Quem gere esses dados e o
  main.py. Aqui e so visualizacao.
- Nao importa o executor.py (que depende de 'solders', uma biblioteca
  nativa em Rust). A cotacao Jupiter e refeita aqui so com 'requests',
  que e pure-Python e ja esta instalada.

Como correr:
    python3 dashboard.py
e depois abrir http://localhost:5000 no browser.
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
from flask import (Flask, jsonify, redirect, render_template,
                   render_template_string, request, session, url_for)

import config  # so para ler DRY_RUN, SALDO_VIRTUAL_INICIAL, etc.

app = Flask(__name__)


# ==========================================================================
# Autenticacao (protege as rotas de dinheiro contra acesso na rede)
# ==========================================================================
# A chave de sessao assina o cookie de login. Guardamo-la num ficheiro
# local (.dashboard_secret, no .gitignore) para as sessoes sobreviverem a
# reinicios do dashboard - senao cada reinicio deslogava toda a gente.
def _obter_secret_key() -> bytes:
    caminho = ".dashboard_secret"
    try:
        if os.path.exists(caminho):
            with open(caminho, "rb") as f:
                dados = f.read().strip()
                if len(dados) >= 32:
                    return dados
        chave = secrets.token_bytes(48)
        with open(caminho, "wb") as f:
            f.write(chave)
        try:
            os.chmod(caminho, 0o600)  # so o dono le/escreve
        except OSError:
            pass
        return chave
    except OSError:
        # Sem disco de escrita -> chave efemera (sessoes nao sobrevivem a
        # reinicios, mas o login continua a funcionar dentro da sessao)
        return secrets.token_bytes(48)


app.secret_key = _obter_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # o cookie nao e acessivel via JS (anti-XSS)
    SESSION_COOKIE_SAMESITE="Lax",  # nao viaja em pedidos cross-site
)


@app.before_request
def _exigir_login():
    """Bloqueia tudo o que nao seja o login/estaticos quando ha password
    definida. Sem password, o proprio arranque forca 127.0.0.1 (localhost),
    por isso nao ha nada exposto a proteger e deixamos passar."""
    if not config.DASHBOARD_PASSWORD:
        return None  # modo localhost-only: sem barreira de login
    if request.endpoint == "static" or request.path == "/login":
        return None  # o form de login e os estaticos ficam acessiveis
    if session.get("autenticado"):
        return None
    # Nao autenticado: API responde 401 (JSON), paginas vao para o login
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "erro": "Sessão expirada ou não autenticada."}), 401
    return redirect(url_for("login"))


_LOGIN_HTML = """<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — Dashboard do Bot</title>
<style>
  body{background:#0f1216;color:#e6e9ef;font-family:system-ui,sans-serif;
       display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
  form{background:#171b22;padding:32px;border-radius:14px;width:min(90vw,340px);
       box-shadow:0 8px 40px rgba(0,0,0,.4)}
  h1{font-size:1.1rem;margin:0 0 4px}
  p{color:#8b94a3;font-size:.85rem;margin:0 0 20px}
  input{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;
        border:1px solid #2a303a;background:#0f1216;color:#e6e9ef;font-size:1rem}
  button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:8px;
         background:#3987e5;color:#fff;font-size:1rem;font-weight:600;cursor:pointer}
  .erro{color:#ff5c7a;font-size:.85rem;margin-top:12px;text-align:center}
</style></head><body>
<form method="post" autocomplete="off">
  <h1>🔒 Dashboard do Bot</h1>
  <p>Esta consola controla dinheiro real. Introduz a password.</p>
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Entrar</button>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    """Pagina de login. Sem password configurada, nao ha login a fazer."""
    if not config.DASHBOARD_PASSWORD:
        return redirect("/")
    erro = None
    if request.method == "POST":
        candidata = request.form.get("password", "")
        # compare_digest evita distinguir passwords pelo tempo de resposta
        if secrets.compare_digest(candidata, config.DASHBOARD_PASSWORD):
            session["autenticado"] = True
            session.permanent = True
            return redirect("/")
        erro = "Password errada."
    return render_template_string(_LOGIN_HTML, erro=erro)


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login") if config.DASHBOARD_PASSWORD else "/")

# Cache de cotacoes: {mint: (timestamp_da_consulta, valor_usd)}
# Evita bombardear a API da Jupiter - cada mint e consultado no maximo
# uma vez a cada CACHE_SEGUNDOS, mesmo que a pagina faca polling rapido.
_cache_cotacoes: dict = {}
CACHE_SEGUNDOS = 30


# ==========================================================================
# Leitura segura dos ficheiros JSON
# ==========================================================================
def _ler_json(caminho: str, defeito):
    """Le um ficheiro JSON. Se nao existir (bot nunca correu) ou estiver
    corrompido, devolve o valor por defeito em vez de rebentar."""
    if not os.path.exists(caminho):
        return defeito
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return defeito


def _ler_carteira() -> dict:
    """carteira.json - saldo virtual + historico de compras/vendas."""
    return _ler_json("carteira.json", {
        "saldo_inicial_usd": config.SALDO_VIRTUAL_INICIAL,
        "saldo_atual_usd": config.SALDO_VIRTUAL_INICIAL,
        "historico": [],
    })


def _ler_posicoes() -> dict:
    """posicoes.json - posicoes abertas neste momento ({mint: {...}})."""
    return _ler_json(config.FICHEIRO_POSICOES, {})


def _ler_radar() -> list:
    """radar.json - ultimos tokens detetados/analisados pelo bot
    (comprados ou nao). Escrito pelo main.py atraves do radar.py."""
    dados = _ler_json("radar.json", [])
    # Protege contra o ficheiro ter sido editado a mao para outro formato
    return dados if isinstance(dados, list) else []


# ==========================================================================
# Cotacao Jupiter (preco atual estimado das posicoes abertas)
# ==========================================================================
def _valor_atual_usd(mint: str, quantidade_tokens: float, chain: str = "solana"):
    """Quanto valem 'quantidade_tokens' deste mint em USD, escolhendo a
    fonte de cotacao pela chain: Jupiter para Solana, PancakeSwap para BSC
    (a Jupiter nao conhece mints 0x... - era por isso que as posicoes BSC
    apareciam sempre como "N/A" no dashboard). Devolve None se a consulta
    falhar (ex: sem internet, token sem liquidez) - o frontend mostra
    "N/A" nesse caso, e a falha fica registada no log do dashboard."""
    agora = time.time()

    # 1) Ja temos um valor recente em cache? Usa esse.
    em_cache = _cache_cotacoes.get(mint)
    if em_cache and (agora - em_cache[0]) < CACHE_SEGUNDOS:
        return em_cache[1]

    # 2a) BSC: cota na PancakeSwap (consulta apenas - nao mexe em dinheiro)
    if chain == "bsc":
        import executor_bsc
        valor = executor_bsc.valor_atual_usd(mint, quantidade_tokens)
        if valor is None:
            # Nunca silenciar: se a cotacao falhar de verdade, fica no log
            print(f"[dashboard] cotacao BSC falhou/sem rota para {mint[:10]}...")
        _cache_cotacoes[mint] = (agora, valor)
        return valor

    # 2b) Solana: pergunta a Jupiter (consulta apenas - nao mexe em dinheiro)
    try:
        resposta = requests.get(config.JUPITER_QUOTE_URL, params={
            "inputMint": mint,
            "outputMint": config.MINT_USDC,
            "amount": int(quantidade_tokens),
            "slippageBps": config.SLIPPAGE_BPS,
        }, timeout=8)
        resposta.raise_for_status()
        # outAmount vem em unidades minimas de USDC (6 casas decimais)
        valor = float(resposta.json()["outAmount"]) / 1_000_000
    except Exception:
        valor = None  # qualquer falha -> "N/A" no dashboard

    _cache_cotacoes[mint] = (agora, valor)
    return valor


# ==========================================================================
# Gestao do processo do bot (main.py como subprocesso)
# ==========================================================================
# O dashboard consegue arrancar e parar o main.py sem precisares do
# terminal. O estado sobrevive a reinicios do dashboard gracas ao
# ficheiro bot.pid: {"pid": ..., "dry_run": ..., "iniciado_em": ...}

FICHEIRO_PID = "bot.pid"
FICHEIRO_LOG = "bot.log"

# Referencia ao Popen quando fomos NOS a arrancar o bot nesta sessao
# do dashboard (se o dashboard reiniciar, recuperamos pelo bot.pid)
_processo_bot = None


def _ler_pid_info():
    """Le o bot.pid. Devolve o dict ou None se nao existir/estiver mau."""
    if not os.path.exists(FICHEIRO_PID):
        return None
    try:
        with open(FICHEIRO_PID, "r", encoding="utf-8") as f:
            info = json.load(f)
            return info if isinstance(info, dict) and "pid" in info else None
    except (json.JSONDecodeError, OSError):
        return None


def _guardar_pid_info(info: dict) -> None:
    """Escrita atomica do bot.pid (tmp + rename, como posicoes.py)."""
    tmp = FICHEIRO_PID + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    os.replace(tmp, FICHEIRO_PID)


def _apagar_pid_info() -> None:
    try:
        os.remove(FICHEIRO_PID)
    except OSError:
        pass


def _pid_vivo(pid: int) -> bool:
    """Verifica se o PID guardado em bot.pid ainda e MESMO o main.py do
    bot, nao qualquer processo.

    BUG CORRIGIDO: so fazer os.kill(pid, 0) nao chega - esse sinal so
    confirma que EXISTE um processo com aquele numero, mas os PIDs sao
    reaproveitados pelo sistema operativo. Se o bot morrer e mais tarde
    outro processo qualquer (sem relacao nenhuma) receber o mesmo PID,
    os.kill(pid, 0) continua a dizer "vivo" - e o indicador "Bot: A
    CORRER" ficava preso, mesmo depois de "Parar Bot" ter funcionado de
    verdade. E exatamente o que foi reportado.

    Correcao: em Linux, confirma tambem que a linha de comando desse PID
    (/proc/<pid>/cmdline) e mesmo o nosso main.py. Sem /proc (ex: outro
    SO), cai de volta ao teste simples - mais fraco, mas nao pior do que
    o comportamento anterior."""
    try:
        os.kill(pid, 0)  # sinal 0: nao mata, so confirma que o PID existe
    except (ProcessLookupError, PermissionError, TypeError):
        return False

    caminho_cmdline = f"/proc/{pid}/cmdline"
    if not os.path.exists(caminho_cmdline):
        return True  # sem /proc (nao-Linux) - aceita o teste simples

    try:
        with open(caminho_cmdline, "rb") as f:
            # argumentos separados por bytes nulos: "python3\0main.py\0"
            cmdline = f.read().decode("utf-8", errors="replace")
        return "main.py" in cmdline
    except OSError:
        # Processo morreu mesmo entre o os.kill e a leitura, ou sem
        # permissao para ler - trata como morto (mais seguro do que
        # assumir vivo e ficar com o indicador preso)
        return False


def _estado_bot():
    """Devolve (a_correr, info_do_pid). Fonte da verdade: o bot.pid +
    verificacao de que o PID ainda esta vivo no sistema."""
    global _processo_bot
    info = _ler_pid_info()
    if info is None:
        return False, None
    if _pid_vivo(info["pid"]):
        return True, info
    # O processo morreu sozinho (crash? Ctrl+C noutro terminal?)
    # -> limpa o pid file para nao ficar estado fantasma
    _apagar_pid_info()
    _processo_bot = None
    return False, None


def _ler_dry_run_do_env() -> bool:
    """Le o valor ATUAL de DRY_RUN diretamente do ficheiro .env.

    Nao usamos config.DRY_RUN aqui porque esse foi lido quando o
    dashboard arrancou - se o .env mudar entretanto (via /api/modo),
    o config ficaria desatualizado. Sem .env ou sem a linha -> True
    (o mesmo defeito seguro do config.py)."""
    if not os.path.exists(".env"):
        return True
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.startswith("DRY_RUN="):
                    valor = linha.split("=", 1)[1].strip().strip('"').strip("'")
                    return valor.lower() in ("1", "true", "yes", "sim")
    except OSError:
        pass
    return True


def _escrever_dry_run_no_env(dry_run: bool) -> None:
    """Atualiza SO a linha DRY_RUN=... do .env, preservando tudo o
    resto (comentarios, outras variaveis). Escrita atomica."""
    valor = "true" if dry_run else "false"
    linhas = []
    substituida = False

    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for linha in f:
                if linha.strip().startswith("DRY_RUN="):
                    linhas.append(f"DRY_RUN={valor}\n")
                    substituida = True
                else:
                    linhas.append(linha)

    if not substituida:
        # .env sem a linha (ou inexistente) -> acrescenta no fim
        if linhas and not linhas[-1].endswith("\n"):
            linhas[-1] += "\n"
        linhas.append(f"DRY_RUN={valor}\n")

    tmp = ".env.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(linhas)
    os.replace(tmp, ".env")


# ==========================================================================
# Rotas
# ==========================================================================
@app.route("/")
def pagina_principal():
    """Serve a pagina HTML do dashboard (templates/index.html)."""
    return render_template("index.html")


@app.route("/api/resumo")
def api_resumo():
    """Devolve tudo o que os cartoes, graficos e tabela de historico
    precisam, calculado a partir do carteira.json."""
    carteira = _ler_carteira()
    posicoes = _ler_posicoes()

    inicial = carteira.get("saldo_inicial_usd", 0.0)
    saldo_livre = carteira.get("saldo_atual_usd", 0.0)
    historico = carteira.get("historico", [])

    # --- Contadores e win rate (so vendas fechadas contam) -------------
    compras = [h for h in historico if h.get("tipo") == "compra"]
    vendas = [h for h in historico if h.get("tipo") == "venda"]
    vendas_com_lucro = [v for v in vendas if v.get("lucro_usd", 0) > 0]
    win_rate = (len(vendas_com_lucro) / len(vendas) * 100) if vendas else None

    # --- Valor investido nas posicoes ainda abertas --------------------
    valor_posicoes = sum(
        p.get("valor_investido_usd", 0.0) for p in posicoes.values()
    )

    # --- Lucro/prejuizo total (saldo livre + posicoes vs. inicial) -----
    valor_total = saldo_livre + valor_posicoes
    lucro_total = valor_total - inicial
    lucro_total_pct = (lucro_total / inicial * 100) if inicial else 0.0

    # --- Curva de saldo: recalcula o saldo apos cada operacao ----------
    # Comeca no saldo inicial; cada compra tira dinheiro, cada venda poe.
    ordenado = sorted(historico, key=lambda h: h.get("timestamp", ""))
    saldo = inicial
    curva = [{"timestamp": None, "saldo": round(saldo, 2)}]  # ponto de partida
    for h in ordenado:
        if h.get("tipo") == "compra":
            saldo -= h.get("valor_usd", 0.0)
        elif h.get("tipo") == "venda":
            saldo += h.get("valor_usd", 0.0)
        curva.append({
            "timestamp": h.get("timestamp"),
            "saldo": round(saldo, 2),
        })

    # --- Ganhos vs perdas (para o grafico de "velas" de P/L) -----------
    total_ganho = sum(v.get("lucro_usd", 0) for v in vendas if v.get("lucro_usd", 0) > 0)
    total_perdido = sum(-v.get("lucro_usd", 0) for v in vendas if v.get("lucro_usd", 0) < 0)
    velas = [  # uma "vela" por venda fechada: sobe se lucro, desce se prejuizo
        {
            "simbolo": v.get("simbolo", "?"),
            "lucro_usd": round(v.get("lucro_usd", 0.0), 4),
            "timestamp": v.get("timestamp"),
        }
        for v in sorted(vendas, key=lambda h: h.get("timestamp", ""))
    ]

    # --- Historico para a tabela (mais recente primeiro) ---------------
    historico_recente = sorted(
        historico, key=lambda h: h.get("timestamp", ""), reverse=True
    )

    return jsonify({
        # Relido do .env a cada pedido (pode mudar via /api/modo)
        "dry_run": _ler_dry_run_do_env(),
        "cartoes": {
            "saldo_inicial": round(inicial, 2),
            "saldo_livre": round(saldo_livre, 2),
            "valor_posicoes": round(valor_posicoes, 2),
            "valor_total": round(valor_total, 2),
            "lucro_total": round(lucro_total, 2),
            "lucro_total_pct": round(lucro_total_pct, 2),
            "n_compras": len(compras),
            "n_vendas": len(vendas),
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
        },
        "curva_saldo": curva,
        "ganhos_perdas": {
            "total_ganho": round(total_ganho, 2),
            "total_perdido": round(total_perdido, 2),
            "velas": velas,
        },
        "historico": historico_recente,
    })


# Cache de decimais por mint (nunca mudam) - evita repetir a chamada RPC
# para posicoes antigas/manuais que nao guardaram os decimais na compra.
_cache_decimais: dict = {}


def _obter_decimais(mint: str, chain: str) -> int | None:
    """Descobre os decimais de um token (para converter o preco por
    unidade minima em preco por token inteiro no dashboard). So usado
    como FALLBACK quando a posicao nao guardou os decimais. Resultado
    fica em cache permanente - decimais nunca mudam. None se falhar."""
    if mint in _cache_decimais:
        return _cache_decimais[mint]

    valor = None
    try:
        if chain == "bsc":
            valor = 18  # padrao ERC-20 - evita uma chamada extra a BSC
        else:
            # Solana: getAccountInfo jsonParsed devolve os decimais do mint
            resp = requests.post(config.SOLANA_RPC_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}],
            }, timeout=8)
            info = resp.json().get("result", {}).get("value", {})
            valor = info.get("data", {}).get("parsed", {}).get("info", {}).get("decimals")
    except Exception:
        valor = None

    _cache_decimais[mint] = valor
    return valor


@app.route("/api/posicoes")
def api_posicoes():
    """Devolve as posicoes abertas com o valor atual estimado (Jupiter).
    Rota separada do /api/resumo porque as cotacoes podem demorar uns
    segundos - assim os cartoes e graficos carregam logo."""
    posicoes = _ler_posicoes()
    lista = []
    for mint, p in posicoes.items():
        quantidade = p.get("quantidade_tokens", 0.0)
        investido = p.get("valor_investido_usd", 0.0)
        preco_compra_raw = p.get("preco_compra_usd", 0.0)

        valor_atual = _valor_atual_usd(mint, quantidade, p.get("chain", "solana")) if quantidade else None

        # Lucro nao realizado = quanto valeria se vendesses agora - investido
        lucro_nao_realizado = (
            round(valor_atual - investido, 2) if valor_atual is not None else None
        )

        # Preco por TOKEN INTEIRO (o que um humano reconhece) = preco por
        # unidade minima * 10^decimais. Os decimais vem da posicao (compras
        # novas) ou de um lookup com cache (posicoes antigas/manuais).
        decimais = p.get("decimais")
        if decimais is None:
            decimais = _obter_decimais(mint, p.get("chain", "solana"))
        preco_compra_por_token = (
            preco_compra_raw * (10 ** decimais) if decimais is not None else None
        )

        # Nivel de stop do trailing puro (so faz sentido nesse modo): pico
        # ja atingido menos TRAILING_PURO_PCT%, convertido para preco por
        # token inteiro (mesma conversao do preco de compra acima).
        pico_raw = p.get("pico_preco_usd")
        pico_por_token = (
            pico_raw * (10 ** decimais) if (pico_raw is not None and decimais is not None) else None
        )
        nivel_stop_trailing_usd = None
        modo_saida_atual = _ler_texto_do_env("MODO_SAIDA", "take_profit_parcial")
        if modo_saida_atual == "trailing_puro" and pico_por_token:
            nivel_stop_trailing_usd = pico_por_token * (1 - config.TRAILING_PURO_PCT / 100)

        lista.append({
            "mint": mint,
            "simbolo": p.get("simbolo", "?"),
            "valor_investido_usd": investido,
            "preco_compra_usd": preco_compra_raw,
            # Preco por token inteiro (para mostrar no dashboard). Se nao
            # soubermos os decimais, fica None e o frontend cai para o raw.
            "preco_compra_por_token_usd": preco_compra_por_token,
            "quantidade_tokens": quantidade,
            "timestamp_compra": p.get("timestamp_compra"),
            "dry_run": p.get("dry_run", True),
            "origem": p.get("origem"),  # "bonding_curve" ou None (compra normal)
            "sniper_rapido": bool(p.get("sniper_rapido", False)),
            "copy": bool(p.get("copy", False)),  # comprado por Copy Trading
            "chain": p.get("chain", "solana"),  # "solana" ou "bsc"
            "dex": p.get("dex"),  # plataforma de origem (pump-fun/raydium/...)
            "modo": p.get("modo", "normal"),  # para o filtro por modo do dashboard
            # Acompanhamento automatico ligado? (defeito True; False = so manual)
            "gestao_automatica": p.get("gestao_automatica", True),
            "valor_atual_usd": round(valor_atual, 2) if valor_atual is not None else None,
            "lucro_nao_realizado_usd": lucro_nao_realizado,
            # So preenchido quando MODO_SAIDA=trailing_puro (None nos outros)
            "nivel_stop_trailing_usd": (
                round(nivel_stop_trailing_usd, 10) if nivel_stop_trailing_usd is not None else None
            ),
        })

    # Mais recente primeiro, como na tabela de historico
    lista.sort(key=lambda p: p.get("timestamp_compra") or "", reverse=True)
    return jsonify({"posicoes": lista})


@app.route("/api/radar")
def api_radar():
    """Devolve os ultimos tokens que o bot detetou e analisou (o radar.py
    ja guarda com o mais recente primeiro e limitado a 50 registos)."""
    return jsonify({"radar": _ler_radar()})


# Rotulos legiveis dos 4 modos de compra (mesma ordem em toda a UI)
_MODOS_CONHECIDOS = ["normal", "bonding_curve", "sniper_rapido", "copy_trading"]
_DEX_DESCONHECIDO = "desconhecido"


def _agrupar_desempenho(historico: list, campo: str, chaves_conhecidas: list | None = None) -> list:
    """Agrupa o historico de compras/vendas por 'campo' (dex ou modo) e
    calcula metricas simples por grupo: nº compras, nº vendas, win rate
    (so entre as vendas) e lucro/prejuizo total (soma de lucro_usd).

    Entradas ANTIGAS do historico (antes desta funcionalidade existir) nao
    tem 'dex'/'modo' - caem no grupo 'desconhecido'/'normal' em vez de
    desaparecerem das estatisticas."""
    grupos: dict = {}

    def _chave(h):
        valor = h.get(campo)
        if valor:
            return valor
        return "normal" if campo == "modo" else _DEX_DESCONHECIDO

    for h in historico:
        chave = _chave(h)
        g = grupos.setdefault(chave, {"n_compras": 0, "n_vendas": 0, "vendas_lucro": 0, "lucro_total": 0.0})
        if h.get("tipo") == "compra":
            g["n_compras"] += 1
        elif h.get("tipo") == "venda":
            g["n_vendas"] += 1
            lucro = h.get("lucro_usd", 0.0) or 0.0
            g["lucro_total"] += lucro
            if lucro > 0:
                g["vendas_lucro"] += 1

    # Ordem estavel: primeiro as chaves conhecidas (ordem fixa), depois o
    # resto por ordem alfabetica - evita a lista "saltar" a cada refresh
    todas_chaves = list(grupos.keys())
    conhecidas = [c for c in (chaves_conhecidas or []) if c in grupos]
    resto = sorted(c for c in todas_chaves if c not in conhecidas)

    resultado = []
    for chave in conhecidas + resto:
        g = grupos[chave]
        win_rate = (g["vendas_lucro"] / g["n_vendas"] * 100) if g["n_vendas"] else None
        resultado.append({
            "chave": chave,
            "n_compras": g["n_compras"],
            "n_vendas": g["n_vendas"],
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
            "lucro_total": round(g["lucro_total"], 2),
        })
    return resultado


@app.route("/api/estatisticas")
def api_estatisticas():
    """Desempenho separado por MODO de compra (Normal/Bonding Curve/Sniper
    Rapido/Copy Trading) e por PLATAFORMA/DEX (pump-fun/raydium/...).

    So usa carteira.json (a carteira VIRTUAL) - tal como o resto do
    /api/resumo, isto reflete a atividade em DRY_RUN. Em modo REAL o
    carteira.json nao e tocado (ver nota em executor_bsc.py), por isso
    estas estatisticas continuam a refletir so o SIMULADO."""
    historico = _ler_carteira().get("historico", [])
    return jsonify({
        "por_modo": _agrupar_desempenho(historico, "modo", _MODOS_CONHECIDOS),
        "por_dex": _agrupar_desempenho(historico, "dex"),
    })


# --------------------------------------------------------------------------
# Rotas de controlo do bot (Parte 1)
# --------------------------------------------------------------------------
@app.route("/api/bot/status")
def api_bot_status():
    """Estado do bot + modo do .env. O frontend usa isto para o
    indicador A CORRER/PARADO, o toggle SIMULADO/REAL e o aviso de
    desfasamento (bot a correr num modo != do que esta no .env)."""
    a_correr, info = _estado_bot()
    dry_run_env = _ler_dry_run_do_env()

    return jsonify({
        "a_correr": a_correr,
        "pid": info["pid"] if a_correr else None,
        "iniciado_em": info.get("iniciado_em") if a_correr else None,
        # Modo com que o bot FOI ARRANCADO (gravado no bot.pid)
        "dry_run_bot": info.get("dry_run") if a_correr else None,
        # Modo que esta AGORA no .env (o que o bot usara no proximo arranque)
        "dry_run_env": dry_run_env,
        # Ha desfasamento? So interessa se o bot estiver a correr
        "desfasado": a_correr and info.get("dry_run") is not None
                      and info.get("dry_run") != dry_run_env,
        # Pre-requisitos (o frontend desativa botoes conforme isto)
        "camada1_ok": config.camada1_configurada(),
        "fase2_ok": config.fase2_configurada(),
        # Multi-chain: redes ativas (para o badge e o filtro do dashboard)
        "redes_ativas": config.REDES_ATIVAS,
    })


@app.route("/api/bot/iniciar", methods=["POST"])
def api_bot_iniciar():
    """Arranca o main.py em segundo plano, com o output para bot.log."""
    global _processo_bot

    # 1) Nunca deixar dois bots a correr ao mesmo tempo
    a_correr, _ = _estado_bot()
    if a_correr:
        return jsonify({"ok": False, "erro": "O bot já está a correr."}), 409

    # 2) Sem Camada 1 configurada o bot nao analisa nada - nao vale a
    #    pena arrancar (mesma verificacao que farias no terminal)
    if not config.camada1_configurada():
        return jsonify({
            "ok": False,
            "erro": "Camada 1 (DeepSeek) não configurada — define DEEPSEEK_API_KEY no .env primeiro.",
        }), 400

    dry_run_atual = _ler_dry_run_do_env()

    # 3) Arranca o main.py:
    #    - sys.executable = o mesmo python3 que corre o dashboard
    #    - stdout+stderr para bot.log (a caixa "Log ao vivo" le daqui)
    #    - start_new_session=True: o bot fica no seu proprio grupo de
    #      processos, por isso sobrevive se o dashboard for reiniciado
    log = open(FICHEIRO_LOG, "a", encoding="utf-8")
    log.write(f"\n===== Bot iniciado pelo dashboard em {datetime.now(timezone.utc).isoformat()} =====\n")
    log.flush()
    _processo_bot = subprocess.Popen(
        [sys.executable, "-u", "main.py"],  # -u = output sem buffer (log em tempo real)
        stdout=log, stderr=subprocess.STDOUT,
    )
    log.close()  # o filho herdou o descritor; podemos fechar o nosso

    # 4) Guarda o PID (+ o modo com que arrancou) para recuperar estado
    _guardar_pid_info({
        "pid": _processo_bot.pid,
        "dry_run": dry_run_atual,
        "iniciado_em": datetime.now(timezone.utc).isoformat(),
    })

    return jsonify({"ok": True, "pid": _processo_bot.pid, "dry_run": dry_run_atual})


@app.route("/api/bot/parar", methods=["POST"])
def api_bot_parar():
    """Para o bot com SIGTERM (saida limpa); SIGKILL so como ultimo
    recurso se ele nao responder em 10 segundos."""
    global _processo_bot

    a_correr, info = _estado_bot()
    if not a_correr:
        return jsonify({"ok": False, "erro": "O bot não está a correr."}), 409

    pid = info["pid"]
    try:
        os.kill(pid, signal.SIGTERM)  # o main.py apanha isto e sai limpo
    except ProcessLookupError:
        pass  # ja morreu entre o _estado_bot() e agora - tudo bem

    # Espera ate 10s pela saida limpa (verifica 20x a cada 0.5s)
    for _ in range(20):
        if not _pid_vivo(pid):
            break
        time.sleep(0.5)
    else:
        # Nao saiu a bem -> forca (SIGKILL nao pode ser ignorado)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    _apagar_pid_info()
    _processo_bot = None
    return jsonify({"ok": True})


@app.route("/api/bot/log")
def api_bot_log():
    """Ultimas 30 linhas do bot.log, para a caixa 'Log ao vivo'."""
    if not os.path.exists(FICHEIRO_LOG):
        return jsonify({"linhas": []})
    try:
        with open(FICHEIRO_LOG, "r", encoding="utf-8", errors="replace") as f:
            linhas = f.readlines()
        return jsonify({"linhas": [l.rstrip("\n") for l in linhas[-30:]]})
    except OSError:
        return jsonify({"linhas": []})


# --------------------------------------------------------------------------
# Watchlist (tokens fronteira) e acoes de trading manual
# --------------------------------------------------------------------------
def _carregar_executor():
    """Importa o executor SO quando e preciso (lazy import).

    O executor puxa o 'solders' (biblioteca nativa) atraves do wallet.py.
    Importa-lo aqui em cima faria o dashboard rebentar em maquinas onde
    o solders nao esta instalado - e para VER os dados ele nao e preciso,
    so para as acoes de compra/venda."""
    import executor  # noqa - import local proposital
    return executor


def _exigir_confirmo_em_modo_real(corpo: dict):
    """Em modo REAL, qualquer acao de trading manual exige a palavra
    CONFIRMO no proprio pedido (mesma defesa em camadas do /api/modo:
    o browser nao e a unica barreira). Devolve uma resposta de erro
    ou None se estiver tudo bem."""
    if _ler_dry_run_do_env():
        return None  # modo simulado: dinheiro falso, sem barreira extra
    if corpo.get("confirmacao") != "CONFIRMO":
        return jsonify({
            "ok": False,
            "erro": "Modo REAL: esta ação usa dinheiro verdadeiro e exige a palavra CONFIRMO.",
        }), 400
    return None


@app.route("/api/historico/<timestamp>", methods=["DELETE"])
def api_remover_historico(timestamp):
    """Remove uma entrada do historico (identificada pelo timestamp).
    So limpeza visual - NAO mexe no saldo. Acao irreversivel (o frontend
    pede confirmacao antes de chamar)."""
    import carteira
    if carteira.remover_do_historico(timestamp):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "Entrada não encontrada no histórico."}), 404


@app.route("/api/posicao/gestao", methods=["POST"])
def api_posicao_gestao():
    """Liga/desliga o acompanhamento automatico (stop-loss/take-profit/
    trailing) de UMA posicao. {"mint": ..., "automatica": true/false}.
    So se aplica a posicoes ja abertas - guardado no proprio posicoes.json."""
    import posicoes
    corpo = request.get_json(silent=True) or {}
    mint = corpo.get("mint", "")
    if not mint or mint not in posicoes.listar_posicoes_abertas():
        return jsonify({"ok": False, "erro": "Posição não encontrada."}), 404
    posicoes.atualizar_posicao(mint, gestao_automatica=bool(corpo.get("automatica", True)))
    return jsonify({"ok": True})


@app.route("/api/watchlist")
def api_watchlist():
    """Tokens fronteira (score perto do limiar) a espera de decisao manual."""
    import watchlist
    return jsonify({"watchlist": watchlist.carregar_watchlist()})


@app.route("/api/watchlist/seguir", methods=["POST"])
def api_watchlist_seguir():
    """Marca/desmarca um token como 'seguido' (nunca sai da watchlist
    por antiguidade enquanto estiver seguido)."""
    import watchlist
    corpo = request.get_json(silent=True) or {}
    mint = corpo.get("mint", "")
    if not mint:
        return jsonify({"ok": False, "erro": "Pedido inválido: falta o mint."}), 400
    if not watchlist.marcar_seguir(mint, bool(corpo.get("seguir", True))):
        return jsonify({"ok": False, "erro": "Token já não está na watchlist."}), 404
    return jsonify({"ok": True})


@app.route("/api/comprar", methods=["POST"])
def api_comprar():
    """Compra manual (a partir da watchlist): MAX_TRADE_USD do token.
    Em modo REAL exige a palavra CONFIRMO no pedido."""
    corpo = request.get_json(silent=True) or {}
    mint = corpo.get("mint", "")
    if not mint:
        return jsonify({"ok": False, "erro": "Pedido inválido: falta o mint."}), 400

    erro = _exigir_confirmo_em_modo_real(corpo)
    if erro:
        return erro

    if mint in _ler_posicoes():
        return jsonify({"ok": False, "erro": "Já existe uma posição aberta neste token."}), 409

    import watchlist
    entrada = next(
        (r for r in watchlist.carregar_watchlist() if r.get("mint") == mint), None
    )
    simbolo = (entrada or {}).get("simbolo") or corpo.get("simbolo") or "?"
    # A chain vem da watchlist (BSC vs Solana) - decide o executor certo
    chain = (entrada or {}).get("chain") or corpo.get("chain") or "solana"
    # dex so para as estatisticas do dashboard; modo "normal" porque uma
    # compra manual da watchlist usa o mesmo caminho de execucao normal,
    # so saltou o gate automatico do score (decisao humana em vez do bot)
    dex = (entrada or {}).get("dex")

    try:
        if chain == "bsc":
            import executor_bsc
            resultado = executor_bsc.comprar_token(mint, simbolo, valor_usd=config.BSC_MAX_TRADE_USD, dex=dex)
        else:
            executor = _carregar_executor()
            # Preco do SOL para converter MAX_TRADE_USD em lamports
            cot_sol = executor._obter_cotacao(config.MINT_SOL, config.MINT_USDC, 1_000_000_000)
            preco_sol_usd = float(cot_sol["outAmount"]) / 1_000_000
            resultado = executor.comprar_token(
                mint=mint, simbolo=simbolo,
                valor_usd=config.MAX_TRADE_USD, preco_sol_usd=preco_sol_usd,
                dex=dex, modo="normal",
            )
    except ImportError:
        return jsonify({"ok": False, "erro": "Dependências do bot em falta (solders/eth-account) — instala requirements.txt."}), 500
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Falha na compra: {e}"}), 500

    if resultado.get("sucesso"):
        watchlist.remover(mint)  # comprado -> sai da watchlist (passa a posicao)
        try:
            import telegram_alerts
            telegram_alerts.enviar(f"✅ COMPRA (manual): {resultado.get('mensagem', '')}")
        except Exception:
            pass
    return jsonify({"ok": bool(resultado.get("sucesso")),
                    "mensagem": resultado.get("mensagem", ""),
                    "dry_run": resultado.get("dry_run")})


@app.route("/api/vender", methods=["POST"])
def api_vender():
    """Venda manual de uma posicao aberta: {"mint": ..., "percentagem": 50|100}.
    Em modo REAL exige a palavra CONFIRMO no pedido."""
    corpo = request.get_json(silent=True) or {}
    mint = corpo.get("mint", "")
    try:
        percentagem = float(corpo.get("percentagem", 0))
    except (TypeError, ValueError):
        percentagem = 0
    if not mint or not (0 < percentagem <= 100):
        return jsonify({"ok": False, "erro": "Pedido inválido: preciso de mint e percentagem (1-100)."}), 400

    erro = _exigir_confirmo_em_modo_real(corpo)
    if erro:
        return erro

    posicoes_abertas = _ler_posicoes()
    if mint not in posicoes_abertas:
        return jsonify({"ok": False, "erro": "Não há posição aberta neste token."}), 404

    # A chain da propria posicao decide o executor (PancakeSwap vs Jupiter)
    chain = posicoes_abertas[mint].get("chain", "solana")
    try:
        if chain == "bsc":
            import executor_bsc
            resultado = executor_bsc.vender_token(mint, percentagem, motivo_venda="manual")
        else:
            executor = _carregar_executor()
            resultado = executor.vender_token(mint, percentagem, motivo_venda="manual")
    except ImportError:
        return jsonify({"ok": False, "erro": "Dependências do bot em falta (solders/eth-account) — instala requirements.txt."}), 500
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Falha na venda: {e}"}), 500

    if resultado.get("sucesso"):
        try:
            import telegram_alerts
            telegram_alerts.enviar(f"💰 VENDA (manual, {percentagem:.0f}%): {resultado.get('mensagem', '')}")
        except Exception:
            pass
    return jsonify({"ok": bool(resultado.get("sucesso")),
                    "mensagem": resultado.get("mensagem", ""),
                    "dry_run": resultado.get("dry_run")})


# --------------------------------------------------------------------------
# Carteira do bot (endereco para depositos + saldo SOL + QR code)
# --------------------------------------------------------------------------
# Cache: o endereco nunca muda e o saldo nao precisa de refrescar a cada
# polling de 4s - guardamos o resultado durante CACHE_WALLET_SEGUNDOS.
_cache_wallet = {"quando": 0.0, "resposta": None}
CACHE_WALLET_SEGUNDOS = 30


def _gerar_qr_svg(texto: str) -> str | None:
    """Gera um QR code do endereco em SVG (texto), server-side.

    Usa a biblioteca 'qrcode' que e pure-Python (zero compilacao, ideal
    para Termux) e nem precisa do pillow no modo SVG. Se a biblioteca
    nao estiver instalada, devolve None e o dashboard mostra so o texto."""
    try:
        import io
        import qrcode
        import qrcode.image.svg
        imagem = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = io.BytesIO()
        imagem.save(buffer)
        return buffer.getvalue().decode("utf-8")
    except Exception:
        return None


@app.route("/api/wallet")
def api_wallet():
    """Endereco publico da wallet do bot (para enviares SOL de outra
    exchange/wallet) + saldo atual em SOL + QR code do endereco.

    A CHAVE PRIVADA NUNCA sai daqui - so o endereco publico.
    Sem nenhuma wallet (Solana nem BSC) no .env -> {"configurada": false}."""
    # Se so a BSC estiver configurada, mostramos so a parte BSC
    if not config.fase2_configurada():
        bsc = _wallet_bsc_info()
        if bsc:
            return jsonify({"configurada": True, "so_bsc": True, "bsc": bsc,
                            "endereco": None, "saldo_sol": None, "qr_svg": None})
        return jsonify({"configurada": False})

    # Cache de 30s (o endereco e fixo; o saldo nao muda a cada 4s)
    agora = time.time()
    if _cache_wallet["resposta"] and (agora - _cache_wallet["quando"]) < CACHE_WALLET_SEGUNDOS:
        return jsonify(_cache_wallet["resposta"])

    # Import lazy: o wallet.py puxa o solders (biblioteca nativa); so o
    # carregamos quando esta rota e mesmo usada, como nas rotas de trading
    try:
        import wallet
        endereco = wallet.endereco_publico()
    except Exception as e:
        # Chave invalida ou solders em falta -> estado vazio com explicacao
        return jsonify({"configurada": False, "erro": str(e)})

    try:
        saldo_sol = wallet.obter_saldo_sol()
    except Exception:
        saldo_sol = None  # RPC falhou; mostra "N/A" e tenta no proximo ciclo

    resposta = {
        "configurada": True,
        "endereco": endereco,
        "saldo_sol": saldo_sol,
        "qr_svg": _gerar_qr_svg(endereco),
        # Wallet BSC (se configurada) - endereco + saldo BNB
        "bsc": _wallet_bsc_info(),
    }
    _cache_wallet["quando"] = agora
    _cache_wallet["resposta"] = resposta
    return jsonify(resposta)


def _wallet_bsc_info():
    """Info da wallet BSC (endereco 0x + saldo BNB), ou None se nao
    configurada. Import lazy do eth-account, tolerante a falha."""
    if not config.fase2_bsc_configurada():
        return None
    try:
        import wallet_bsc
        endereco = wallet_bsc.endereco_publico()
    except Exception:
        return None
    try:
        saldo = wallet_bsc.obter_saldo_bnb()
    except Exception:
        saldo = None
    return {"endereco": endereco, "saldo_bnb": saldo}


# --------------------------------------------------------------------------
# Rota de mudanca de modo SIMULADO <-> REAL (Parte 3)
# --------------------------------------------------------------------------
def _ler_bool_do_env(chave: str, defeito: bool = False) -> bool:
    """Le um booleano diretamente do .env (relido a cada pedido, como o
    DRY_RUN). Sem a linha -> devolve o defeito."""
    if not os.path.exists(".env"):
        return defeito
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.startswith(f"{chave}="):
                    valor = linha.split("=", 1)[1].strip().strip('"').strip("'")
                    return valor.lower() in ("1", "true", "yes", "sim")
    except OSError:
        pass
    return defeito


def _escrever_bool_no_env(chave: str, valor: bool) -> None:
    """Atualiza SO a linha 'chave=...' do .env (atomico), preservando o
    resto. Reutiliza a mesma logica do DRY_RUN mas para qualquer chave."""
    _escrever_texto_no_env(chave, "true" if valor else "false")


def _ler_texto_do_env(chave: str, defeito: str) -> str:
    """Le uma chave de texto livre do .env (relida a cada pedido). Sem a
    linha -> devolve o defeito. Usada para MODO_SAIDA (nao e booleano)."""
    if not os.path.exists(".env"):
        return defeito
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.startswith(f"{chave}="):
                    return linha.split("=", 1)[1].strip().strip('"').strip("'") or defeito
    except OSError:
        pass
    return defeito


def _escrever_texto_no_env(chave: str, texto: str) -> None:
    """Atualiza SO a linha 'chave=...' do .env (atomico), preservando o
    resto."""
    linhas = []
    substituida = False
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for linha in f:
                if linha.strip().startswith(f"{chave}="):
                    linhas.append(f"{chave}={texto}\n")
                    substituida = True
                else:
                    linhas.append(linha)
    if not substituida:
        if linhas and not linhas[-1].endswith("\n"):
            linhas[-1] += "\n"
        linhas.append(f"{chave}={texto}\n")
    tmp = ".env.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(linhas)
    os.replace(tmp, ".env")


@app.route("/api/pumpfun")
def api_pumpfun():
    """Estado do modo 'compra na bonding curve' (experimental)."""
    return jsonify({
        "ativo": _ler_bool_do_env("PUMPFUN_BONDING_CURVE_ATIVO", False),
        # So informativo: quanto arrisca por trade e o limiar apertado
        "max_trade_usd": config.PUMPFUN_MAX_TRADE_USD,
        "score_max": config.PUMPFUN_SCORE_COMPRA_MAX,
    })


@app.route("/api/pumpfun", methods=["POST"])
def api_pumpfun_toggle():
    """Liga/desliga o modo curva. Ligar (a direcao arriscada) exige a
    palavra CONFIRMO no pedido - a validacao vive no BACKEND, tal como
    a mudanca para modo REAL. Desligar e sempre livre."""
    corpo = request.get_json(silent=True) or {}
    if "ativo" not in corpo:
        return jsonify({"ok": False, "erro": "Pedido inválido: falta 'ativo'."}), 400
    ativar = bool(corpo["ativo"])

    if ativar and corpo.get("confirmacao") != "CONFIRMO":
        return jsonify({
            "ok": False,
            "erro": "Ativar a compra na bonding curve exige escrever CONFIRMO (risco de perda total).",
        }), 400

    _escrever_bool_no_env("PUMPFUN_BONDING_CURVE_ATIVO", ativar)
    a_correr, _ = _estado_bot()
    return jsonify({"ok": True, "ativo": ativar, "precisa_reiniciar": a_correr})


@app.route("/api/sniper")
def api_sniper():
    """Estado do Modo Sniper Rapido (o mais arriscado dos 3 modos de
    compra) + quanto ja gastou hoje do limite diario obrigatorio."""
    import sniper_rapido
    return jsonify({
        "ativo": _ler_bool_do_env("SNIPER_RAPIDO_ATIVO", False),
        "valor_usd": config.SNIPER_RAPIDO_VALOR_USD,
        # Criterios da checklist binaria (substituiram o score heuristico)
        "liquidez_minima_usd": config.LIQUIDEZ_MINIMA_CAVEIRA_USD,
        "idade_maxima_seg": config.IDADE_MAXIMA_CAVEIRA_SEGUNDOS,
        "limite_diario_usd": config.SNIPER_RAPIDO_LIMITE_DIARIO_USD,
        "gasto_hoje_usd": round(sniper_rapido.gasto_hoje_usd(), 2),
        "restante_hoje_usd": round(sniper_rapido.restante_hoje_usd(), 2),
        # Filtro de qualidade (momentum) - para o dashboard mostrar o
        # trade-off de velocidade junto ao toggle
        "momentum_janela_seg": config.CAVEIRA_JANELA_MOMENTUM_SEGUNDOS,
        "momentum_ativo": (
            config.CAVEIRA_RATIO_COMPRA_VENDA_MIN > 0
            or config.CAVEIRA_COMPRADORES_UNICOS_MIN > 0
            or config.CAVEIRA_TRANSACOES_MIN > 0
            or 0 < config.CAVEIRA_TOP_HOLDER_MAX_PCT < 100
        ),
    })


@app.route("/api/sniper", methods=["POST"])
def api_sniper_toggle():
    """Liga/desliga o Modo Sniper Rapido. Ligar exige SEMPRE a palavra
    CONFIRMO no pedido - a validacao final vive aqui no backend, mesmo
    que o frontend peca uma confirmacao dupla em modo REAL (o mesmo
    padrao usado para mudar de SIMULADO para REAL). Desligar e livre."""
    corpo = request.get_json(silent=True) or {}
    if "ativo" not in corpo:
        return jsonify({"ok": False, "erro": "Pedido inválido: falta 'ativo'."}), 400
    ativar = bool(corpo["ativo"])

    if ativar and corpo.get("confirmacao") != "CONFIRMO":
        return jsonify({
            "ok": False,
            "erro": "Ativar o Modo Sniper Rápido exige escrever CONFIRMO — é o modo mais arriscado do bot.",
        }), 400

    _escrever_bool_no_env("SNIPER_RAPIDO_ATIVO", ativar)
    a_correr, _ = _estado_bot()
    return jsonify({"ok": True, "ativo": ativar, "precisa_reiniciar": a_correr})


@app.route("/api/copy")
def api_copy():
    """Estado do Copy Trading: toggle, carteiras seguidas, valor por copia
    e quanto ja gastou hoje do limite diario obrigatorio."""
    import copy_trade
    carteiras = copy_trade.carteiras_configuradas()
    return jsonify({
        "ativo": _ler_bool_do_env("COPY_TRADE_ATIVO", False),
        "carteiras": carteiras,
        "n_carteiras": len(carteiras),
        "valor_usd": config.COPY_TRADE_VALOR_USD,
        "limite_diario_usd": config.COPY_TRADE_LIMITE_DIARIO_USD,
        "gasto_hoje_usd": round(copy_trade.gasto_hoje_usd(), 2),
        "restante_hoje_usd": round(copy_trade.restante_hoje_usd(), 2),
    })


@app.route("/api/copy", methods=["POST"])
def api_copy_toggle():
    """Liga/desliga o Copy Trading. Ligar exige a palavra CONFIRMO (mesmo
    padrao dos outros modos de compra) E pelo menos uma carteira na lista
    COPY_TRADE_WALLETS. Desligar e livre."""
    corpo = request.get_json(silent=True) or {}
    if "ativo" not in corpo:
        return jsonify({"ok": False, "erro": "Pedido inválido: falta 'ativo'."}), 400
    ativar = bool(corpo["ativo"])

    if ativar:
        if corpo.get("confirmacao") != "CONFIRMO":
            return jsonify({
                "ok": False,
                "erro": "Ativar o Copy Trading exige escrever CONFIRMO — replica compras de carteiras alheias.",
            }), 400
        import copy_trade
        if not copy_trade.carteiras_configuradas():
            return jsonify({
                "ok": False,
                "erro": "Sem carteiras para seguir. Preenche COPY_TRADE_WALLETS no .env primeiro.",
            }), 400

    _escrever_bool_no_env("COPY_TRADE_ATIVO", ativar)
    a_correr, _ = _estado_bot()
    return jsonify({"ok": True, "ativo": ativar, "precisa_reiniciar": a_correr})


@app.route("/api/tokens_carteira")
def api_tokens_carteira():
    """Lista os tokens SPL que a wallet do bot detem agora (simbolo,
    quantidade e valor USD quando o RPC e Helius). So leitura."""
    try:
        import carteira_tokens
        return jsonify(carteira_tokens.listar_tokens())
    except ImportError:
        return jsonify({"configurada": False, "erro": "solders não instalado.", "tokens": []})
    except Exception as e:
        return jsonify({"configurada": False, "erro": str(e), "tokens": []})


@app.route("/api/enviar_token", methods=["POST"])
def api_enviar_token():
    """Envia um token SPL da wallet do bot para outro endereco.
    ACAO IRREVERSIVEL. Em modo REAL exige a palavra CONFIRMO (alem da
    trava PERMITIR_ENVIO_TOKENS que o proprio modulo verifica)."""
    corpo = request.get_json(silent=True) or {}
    mint = corpo.get("mint", "")
    destino = corpo.get("destino", "")
    try:
        quantidade = float(corpo.get("quantidade", 0))
    except (TypeError, ValueError):
        quantidade = 0
    if not mint or not destino or quantidade <= 0:
        return jsonify({"ok": False, "erro": "Preciso de mint, destino e quantidade (> 0)."}), 400

    # Em modo REAL, a mesma barreira CONFIRMO das outras acoes de dinheiro
    erro = _exigir_confirmo_em_modo_real(corpo)
    if erro:
        return erro

    try:
        import carteira_tokens
        r = carteira_tokens.enviar_token(mint, destino, quantidade)
    except ImportError:
        return jsonify({"ok": False, "erro": "solders não instalado — instala as dependências do bot."}), 500
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Falha no envio: {e}"}), 500

    return jsonify({"ok": bool(r.get("sucesso")), "mensagem": r.get("mensagem", ""),
                    "dry_run": r.get("dry_run")})


@app.route("/api/modo", methods=["POST"])
def api_modo():
    """Muda DRY_RUN no .env. A validacao critica vive AQUI no backend:
    mesmo que alguem contorne os dialogos do browser, mudar para REAL
    exige wallet configurada + a palavra CONFIRMO no proprio pedido."""
    corpo = request.get_json(silent=True) or {}
    if "dry_run" not in corpo:
        return jsonify({"ok": False, "erro": "Pedido inválido: falta o campo dry_run."}), 400

    novo_dry_run = bool(corpo["dry_run"])

    # ---- Mudar para REAL: as duas barreiras de seguranca ----
    if not novo_dry_run:
        # Barreira 1: sem wallet nao ha modo real
        if not config.fase2_configurada():
            return jsonify({
                "ok": False,
                "erro": "WALLET_PRIVATE_KEY não está configurada no .env — sem wallet não é possível ativar o modo REAL.",
            }), 400
        # Barreira 2: a palavra de confirmacao tem de vir no pedido
        if corpo.get("confirmacao") != "CONFIRMO":
            return jsonify({
                "ok": False,
                "erro": "Confirmação em falta: para mudar para modo REAL é preciso escrever CONFIRMO.",
            }), 400

    _escrever_dry_run_no_env(novo_dry_run)

    # Se o bot estiver a correr, a mudanca so se aplica no reinicio
    a_correr, _ = _estado_bot()
    return jsonify({
        "ok": True,
        "dry_run": novo_dry_run,
        "precisa_reiniciar": a_correr,
    })


@app.route("/api/modo_saida")
def api_modo_saida():
    """Estado do modo de SAIDA de uma posicao (como sair de uma posicao a
    subir): 'take_profit_parcial' (default) ou 'trailing_puro'."""
    return jsonify({
        "modo_saida": _ler_texto_do_env("MODO_SAIDA", "take_profit_parcial"),
        "trailing_puro_pct": config.TRAILING_PURO_PCT,
        "take_profit_multiplicador": config.TAKE_PROFIT_MULTIPLICADOR,
        "take_profit_vender_pct": config.TAKE_PROFIT_VENDER_PCT,
        "trailing_stop_pct": config.TRAILING_STOP_PCT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
    })


@app.route("/api/modo_saida", methods=["POST"])
def api_modo_saida_toggle():
    """Muda MODO_SAIDA no .env. So aceita os 2 valores validos - o STOP_LOSS_PCT
    normal (desde o preco de compra) continua ativo em AMBOS os modos."""
    corpo = request.get_json(silent=True) or {}
    modo = corpo.get("modo_saida")
    if modo not in ("take_profit_parcial", "trailing_puro"):
        return jsonify({
            "ok": False,
            "erro": "modo_saida tem de ser 'take_profit_parcial' ou 'trailing_puro'.",
        }), 400

    _escrever_texto_no_env("MODO_SAIDA", modo)
    a_correr, _ = _estado_bot()
    return jsonify({"ok": True, "modo_saida": modo, "precisa_reiniciar": a_correr})


@app.route("/api/resetar", methods=["POST"])
def api_resetar():
    """Reset a frio: limpa carteira.json, posicoes.json, radar.json e
    watchlist.json, repondo o saldo virtual inicial. Serve para limpar
    dados de teste (ou corrompidos por um bug ja corrigido).

    SO funciona em modo DRY_RUN - nunca deixa resetar dados quando o
    bot pode estar a gerir dinheiro real. Exige a palavra RESETAR no
    pedido (a mesma defesa em camadas das outras acoes irreversiveis).
    Nao vende as posicoes abertas - fecha-as "a frio" (sem passar pelo
    executor), por isso o bot tem de estar PARADO."""
    corpo = request.get_json(silent=True) or {}

    if not _ler_dry_run_do_env():
        return jsonify({
            "ok": False,
            "erro": "Reset bloqueado: o bot está em modo REAL. Resetar dados com fundos reais envolvidos é perigoso e não é permitido por aqui.",
        }), 400

    a_correr, _ = _estado_bot()
    if a_correr:
        return jsonify({
            "ok": False,
            "erro": "Para regear o bot primeiro: um reset com o bot a correr pode deixar processos a escrever em ficheiros que acabaram de ser limpos. Para o bot antes de resetar.",
        }), 409

    if corpo.get("confirmacao") != "RESETAR":
        return jsonify({
            "ok": False,
            "erro": "Confirmação em falta: escreve RESETAR para limpar todos os dados de teste.",
        }), 400

    # Repoe a carteira virtual ao saldo inicial (nao apaga o ficheiro -
    # reescreve-o limpo, para o saldo inicial configurado ficar visivel)
    tmp = FICHEIRO_CARTEIRA_TMP = "carteira.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "saldo_inicial_usd": config.SALDO_VIRTUAL_INICIAL,
            "saldo_atual_usd": config.SALDO_VIRTUAL_INICIAL,
            "historico": [],
        }, f, indent=2, ensure_ascii=False)
    os.replace(tmp, "carteira.json")

    # Fecha todas as posicoes a frio (sem vender - e um reset, nao uma
    # liquidacao) e limpa radar/watchlist
    for ficheiro, vazio in (
        ("posicoes.json", "{}"),
        ("radar.json", "[]"),
        ("watchlist.json", "[]"),
    ):
        tmp_f = ficheiro + ".tmp"
        with open(tmp_f, "w", encoding="utf-8") as f:
            f.write(vazio)
        os.replace(tmp_f, ficheiro)

    return jsonify({"ok": True, "saldo_inicial": config.SALDO_VIRTUAL_INICIAL})


if __name__ == "__main__":
    modo = "DRY RUN (simulado)" if config.DRY_RUN else "REAL"
    print(f"Dashboard do bot - modo {modo}")

    # SEGURANCA: so escutamos na rede (0.0.0.0, acessivel pelo telemovel)
    # se houver password definida. Sem password, escutamos SO em localhost
    # (127.0.0.1) - o dashboard controla dinheiro real e nao pode ficar
    # aberto a toda a rede sem autenticacao.
    if config.DASHBOARD_PASSWORD:
        host = "0.0.0.0"
        print("Login ATIVO (DASHBOARD_PASSWORD definida).")
        print("Aberto na rede em: http://<IP-do-dispositivo>:5000")
    else:
        host = "127.0.0.1"
        print("Sem DASHBOARD_PASSWORD -> acesso SO local (127.0.0.1).")
        print("Para aceder pelo telemovel, define DASHBOARD_PASSWORD no .env.")
        print("Aberto em: http://localhost:5000")

    # debug=False porque isto pode ficar sempre a correr ao lado do bot.
    app.run(host=host, port=5000, debug=False)
