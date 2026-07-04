/* ==========================================================================
   app.js - logica do dashboard no browser (v3)
   ==========================================================================
   O que este ficheiro faz, por ordem:
     1. Funcoes de formatacao (dinheiro, datas, tempo decorrido)
     2. Sistema de toast (mensagens rapidas) e modais (confirmacoes)
     3. Os dois graficos Chart.js
     4. Controlo do bot: estado, iniciar/parar, toggle SIMULADO/REAL
     5. Polling: a cada 4 segundos pede tudo ao Flask e atualiza a pagina
   JavaScript "puro", sem frameworks - proposital para ser facil de ler.
   ========================================================================== */

// Cores - as mesmas variaveis do style.css, lidas do proprio CSS
// para nunca ficarem dessincronizadas entre graficos e resto da pagina
const css = getComputedStyle(document.documentElement);
const COR_AZUL = css.getPropertyValue("--azul").trim();
const COR_VERDE = css.getPropertyValue("--verde").trim();
const COR_VERMELHO = css.getPropertyValue("--vermelho").trim();
const COR_TEXTO_MUDO = css.getPropertyValue("--texto-mudo").trim();
const COR_GRELHA = css.getPropertyValue("--linha-grelha").trim();

const INTERVALO_POLLING_MS = 4000; // pede dados novos a cada 4 segundos

// ==========================================================================
// 1) Funcoes de formatacao e pequenos utilitarios
// ==========================================================================

/** Atalho para document.getElementById - usado em todo o lado */
function el(id) { return document.getElementById(id); }

/** Escapa uma string para ser inserida com seguranca em HTML (via
    innerHTML) OU dentro de um atributo. SEGURANCA: os simbolos/nomes dos
    tokens vem de fontes externas (GeckoTerminal/on-chain) e qualquer um
    pode criar um token com um nome tipo `<img onerror=...>`. Sem isto,
    esse nome era interpretado como HTML e executava JS no dashboard (XSS).
    O radar mostra TODOS os tokens detetados, por isso bastava o bot correr.
    Nota: ao ler de volta um data-* atributo, o browser desfaz estas
    entidades automaticamente, por isso dataset.* devolve o valor original. */
function escHtml(valor) {
  return String(valor ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** Formata um numero como dinheiro: 12.3456 -> "$12.35" */
function dinheiro(valor) {
  if (valor === null || valor === undefined) return "N/A";
  return "$" + valor.toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Igual, mas com sinal explicito: +$1.20 / -$0.50 (para lucros) */
function dinheiroComSinal(valor) {
  if (valor === null || valor === undefined) return "N/A";
  return (valor >= 0 ? "+" : "-") + dinheiro(Math.abs(valor));
}

// Nunca deixar um preco ocupar mais do que isto na tabela (em caracteres
// visiveis) - acima disto, corta e mostra o valor completo em tooltip
// (desktop) + clique-para-copiar (telemovel, sem hover).
const ORCAMENTO_PRECO_CHARS = 14;

/** Preco unitario de tokens. Numeros minusculos (tipo 7.772e-12) sao a
    grande dor: em notacao cientifica sao ilegiveis. Aqui expandimos para
    decimal SEM notacao cientifica (ex: $0.000000000007772). Para numeros
    normais (>= 0.01) mostra 4 casas. Se o resultado ultrapassar o
    orcamento de ~14 caracteres, corta com "…" e poe o valor completo
    num title (tooltip) + clicavel para copiar. "–" se nao houver dado. */
function precoUnitario(valor) {
  if (valor === null || valor === undefined) return "–";
  const n = Number(valor);
  if (n === 0) return "$0";
  if (n >= 0.01) return "$" + n.toFixed(4);  // sempre curto, nunca precisa de corte

  // Quantos zeros ha entre a virgula e o 1.o digito -> casas necessarias
  const zeros = -Math.floor(Math.log10(n)) - 1;
  let texto = n.toFixed(zeros + 4);
  texto = texto.replace(/0+$/, "");  // tira zeros finais desnecessarios
  const completo = "$" + texto;

  if (completo.length <= ORCAMENTO_PRECO_CHARS) return completo;

  const cortado = completo.slice(0, ORCAMENTO_PRECO_CHARS - 1) + "…";
  return `<span class="preco-truncado" title="${completo}" data-copiar="${completo}">${cortado}</span>`;
}

/** Abrevia quantidades de tokens grandes: 1,000 -> "1K", 8419232150144
    -> "8.42T". Evita numeros gigantes ilegiveis nas tabelas. */
function abreviarQuantidade(valor) {
  if (valor === null || valor === undefined) return "–";
  const n = Number(valor);
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return n.toLocaleString("pt-PT", { maximumFractionDigits: 2 });
}

/** Marketcap/FDV compacto: $1.2M, $45K, $980 ou "–" */
function marketcap(valor) {
  if (valor === null || valor === undefined) return "–";
  const n = Number(valor);
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
  return "$" + n.toFixed(0);
}

/** Formata uma percentagem com sinal: +1,15% / -3,20% */
function percentagemComSinal(valor) {
  const texto = Math.abs(valor).toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (valor >= 0 ? "+" : "-") + texto + "%";
}

/** Converte um timestamp ISO (UTC) para data/hora local legivel */
function dataHora(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleDateString("pt-PT", { day: "2-digit", month: "2-digit" }) +
         " " + d.toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
}

/** "Ha quanto tempo": recebe o ISO e devolve ex. "2h 15m" */
function tempoDecorrido(iso) {
  if (!iso) return "–";
  const segundos = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const dias = Math.floor(segundos / 86400);
  const horas = Math.floor((segundos % 86400) / 3600);
  const minutos = Math.floor((segundos % 3600) / 60);
  if (dias > 0) return `${dias}d ${horas}h`;
  if (horas > 0) return `${horas}h ${minutos}m`;
  return `${minutos}m`;
}

/** Pinta um elemento de verde/vermelho consoante o valor ser +/- */
function pintarPorSinal(elemento, valor) {
  elemento.classList.remove("positivo", "negativo");
  if (valor > 0) elemento.classList.add("positivo");
  if (valor < 0) elemento.classList.add("negativo");
}

/** Escreve texto num elemento COM flash violeta se o valor mudou
    (a micro-animacao dos cartoes de resumo) */
function definirValor(id, texto) {
  const elemento = el(id);
  if (elemento.textContent !== texto) {
    elemento.textContent = texto;
    elemento.classList.remove("flash");
    // Truque: forcar o browser a "ver" a remocao antes de repor a
    // classe, senao a animacao so corria na primeira mudanca
    void elemento.offsetWidth;
    elemento.classList.add("flash");
  }
}

/** Mostra o grafico OU a mensagem de vazio, nunca os dois ao mesmo tempo */
function alternarVazio(canvas, idMensagem, temDados) {
  canvas.closest(".area-grafico").hidden = !temDados;
  el(idMensagem).hidden = temDados;
}

// Filtro de chain ativo no dashboard ("todas" | "solana" | "bsc").
// O polling filtra as tabelas por este valor, sem novo pedido ao servidor.
let filtroChain = "todas";

// Filtros da tabela de historico (todos client-side)
const filtrosHist = { simbolo: "", resultado: "todos", periodo: "tudo" };

/** Aplica os filtros de historico (simbolo, resultado, periodo) a uma linha */
function passaFiltroHist(h) {
  // Simbolo (busca parcial, sem distinguir maiusculas)
  if (filtrosHist.simbolo &&
      !(h.simbolo || "").toLowerCase().includes(filtrosHist.simbolo.toLowerCase())) {
    return false;
  }
  // Resultado (so as vendas tem lucro; compras nao entram nos filtros de lucro)
  if (filtrosHist.resultado === "lucro" && !(h.tipo === "venda" && h.lucro_usd > 0)) return false;
  if (filtrosHist.resultado === "prejuizo" && !(h.tipo === "venda" && h.lucro_usd < 0)) return false;
  // Periodo
  if (filtrosHist.periodo !== "tudo" && h.timestamp) {
    const idadeH = (Date.now() - new Date(h.timestamp).getTime()) / 3600000;
    if (filtrosHist.periodo === "hoje" && idadeH > 24) return false;
    if (filtrosHist.periodo === "7dias" && idadeH > 24 * 7) return false;
  }
  return true;
}

/** True se um registo (com campo .chain) deve aparecer com o filtro atual */
function passaFiltroChain(item) {
  if (filtroChain === "todas") return true;
  return (item.chain || "solana") === filtroChain;
}

// Filtro de MODO ativo ("todos" | "normal" | "bonding_curve" |
// "sniper_rapido" | "copy_trading"). Aplica-se a Posicoes, Radar e
// Historico - a Watchlist fica de fora de proposito: sao tokens ainda
// nao comprados, por isso nao pertencem a nenhum modo especifico ainda.
let filtroModo = "todos";

/** True se um registo (com campo .modo, so definido depois de comprado)
    deve aparecer com o filtro de modo atual. */
function passaFiltroModo(item) {
  if (filtroModo === "todos") return true;
  return item.modo === filtroModo;
}

const _ROTULOS_MODO = {
  normal: "Normal", bonding_curve: "Bonding Curve",
  sniper_rapido: "💀 Sniper Rápido", copy_trading: "👥 Copy Trading",
};
/** Nome legivel de um modo (para as tabelas de estatisticas) */
function rotuloModo(modo) {
  return _ROTULOS_MODO[modo] || modo || "Normal";
}

el("filtro-modo").addEventListener("change", (evento) => {
  filtroModo = evento.target.value;
  atualizarTudo();
});

/** Etiqueta pequena da rede, ao lado do simbolo nas tabelas */
function tagChain(chain) {
  const c = chain || "solana";
  return ` <span class="tag chain-${c}">${c === "bsc" ? "BSC" : "SOL"}</span>`;
}

/** Simbolo clicavel: abre o grafico do token num novo separador.
    DexScreener usa /solana/ ou /bsc/ conforme a chain; para pump.fun
    linka a propria pagina. Sem mint -> devolve so o texto. */
function linkToken(mint, simbolo, dex = "", chain = "solana") {
  const nome = escHtml(simbolo || "?");  // escapado: simbolo vem de fora (XSS)
  if (!mint) return nome;
  const ePumpFun = (dex || "").toLowerCase().includes("pump");
  // encodeURIComponent no mint (parte de um URL); escHtml no href final
  const mintUrl = encodeURIComponent(mint);
  let url;
  if (ePumpFun) url = `https://pump.fun/${mintUrl}`;
  else url = `https://dexscreener.com/${encodeURIComponent(chain || "solana")}/${mintUrl}`;
  // rel="noopener": impede a pagina aberta de controlar o dashboard
  return `<a class="link-token" href="${escHtml(url)}" target="_blank" rel="noopener">${nome} ↗</a>`;
}

// ==========================================================================
// 2) Toast (mensagens rapidas) e modais (confirmacoes)
// ==========================================================================

let _timerToast = null;

/** Mostra uma mensagem temporaria no fundo do ecra.
    tipo: "erro" (vermelho), "sucesso" (verde) ou "" (neutro) */
function toast(mensagem, tipo = "") {
  const caixa = el("toast");
  caixa.textContent = mensagem;
  caixa.className = "toast " + tipo;
  caixa.hidden = false;
  clearTimeout(_timerToast);
  _timerToast = setTimeout(() => { caixa.hidden = true; }, 4000);
}

/** Abre um dos modais (pelo id) por cima do fundo escurecido */
function abrirModal(id) {
  el("modal-fundo").hidden = false;
  // Esconde todos os modais e mostra so o pedido
  document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; });
  el(id).hidden = false;
}

/** Fecha tudo (fundo + modais) e limpa a caixa do CONFIRMO */
function fecharModais() {
  el("modal-fundo").hidden = true;
  el("input-confirmo").value = "";
  el("btn-real-confirmar").disabled = true;
}

// Qualquer botao com o atributo data-fechar cancela o modal
document.querySelectorAll("[data-fechar]").forEach((btn) => {
  btn.addEventListener("click", fecharModais);
});
// Tocar no fundo escurecido (fora do dialogo) tambem cancela
el("modal-fundo").addEventListener("click", (evento) => {
  if (evento.target === el("modal-fundo")) fecharModais();
});

// --- Modal generico de acao de trading (comprar/vender) ---
// Em modo REAL mostra tambem a caixa CONFIRMO (o backend exige a palavra).
let _acaoPendente = null; // funcao a executar quando o utilizador confirmar

function abrirModalAcao(titulo, texto, aoConfirmar) {
  el("acao-titulo").textContent = titulo;
  el("acao-texto").textContent = texto;
  const emReal = estadoBot && !estadoBot.dry_run_env;
  el("acao-aviso-real").hidden = !emReal;
  el("acao-confirmo").hidden = !emReal;
  el("acao-confirmo").value = "";
  _acaoPendente = aoConfirmar;
  abrirModal("modal-acao");
}

el("acao-confirmar").addEventListener("click", () => {
  const emReal = estadoBot && !estadoBot.dry_run_env;
  const palavra = el("acao-confirmo").value.trim();
  if (emReal && palavra !== "CONFIRMO") {
    toast("Escreve CONFIRMO para executar em modo REAL.", "erro");
    return;
  }
  const acao = _acaoPendente;
  _acaoPendente = null;
  fecharModais();
  if (acao) acao(emReal ? palavra : null);
});

// ==========================================================================
// 3) Graficos (comecam vazios, o polling enche-os)
// ==========================================================================

// Opcoes partilhadas: grelha discreta, texto mudo, sem animacao a cada
// refresh (senao os graficos "dancavam" de 4 em 4 segundos)
const opcoesBase = {
  responsive: true,
  maintainAspectRatio: false, // deixa o CSS (.area-grafico) mandar na altura
  animation: false,
  plugins: { legend: { display: false } }, // uma serie so -> o titulo do painel chega
  scales: {
    x: {
      ticks: { color: COR_TEXTO_MUDO, maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 11 } },
      grid: { display: false },
    },
    y: {
      ticks: { color: COR_TEXTO_MUDO, callback: (v) => "$" + v, font: { size: 11 } },
      grid: { color: COR_GRELHA },
    },
  },
};

// --- Grafico 1: curva de saldo (linha) -----------------------------------
const graficoSaldo = new Chart(el("grafico-saldo"), {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      label: "Saldo (USD)",
      data: [],
      borderColor: COR_AZUL,
      backgroundColor: COR_AZUL,
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 5, // alvo de toque maior no telemovel
      tension: 0.15,       // curva ligeiramente suavizada
    }],
  },
  options: opcoesBase,
});

// --- Grafico 2: "velas" de lucro/prejuizo por venda -----------------------
// Cada venda fechada e uma barra que parte de 0: para cima e verde
// (lucro), para baixo e vermelha (prejuizo). Os dados chegam como
// pares [0, lucro] ("floating bars" do Chart.js).
const graficoVelas = new Chart(el("grafico-velas"), {
  type: "bar",
  data: {
    labels: [],
    datasets: [{
      label: "Lucro da venda (USD)",
      data: [],
      backgroundColor: [], // uma cor por barra, definida no refresh
      borderRadius: 4,     // pontas arredondadas
      borderSkipped: false,
      maxBarThickness: 26,
    }],
  },
  options: {
    ...opcoesBase,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          // Titulo do tooltip: simbolo + data/hora da venda (a data vem
          // guardada junto de cada barra, no campo extra "datasVendas")
          title: (itens) => {
            const i = itens[0];
            return `${i.label} · ${graficoVelas.data.datasets[0].datasVendas[i.dataIndex] || ""}`;
          },
          // Corpo: o lucro real, nao o par [0, lucro]
          label: (ctx) => " " + dinheiroComSinal(ctx.raw[1]),
        },
      },
    },
  },
});

// ==========================================================================
// 4) Controlo do bot (Parte 1) e do modo SIMULADO/REAL (Parte 3)
// ==========================================================================

// Ultimo estado recebido de /api/bot/status - os botoes decidem o que
// fazer com base nisto (ex: iniciar em REAL pede confirmacao extra)
let estadoBot = null;

/** POST simples para as rotas de acao; devolve o JSON da resposta */
async function pedirAcao(url, corpo = null) {
  const resposta = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: corpo ? JSON.stringify(corpo) : null,
  });
  return resposta.json();
}

/** Arranca o bot de verdade (chamado apos as confirmacoes necessarias) */
async function iniciarBot() {
  fecharModais();
  const r = await pedirAcao("/api/bot/iniciar");
  if (r.ok) toast("Bot iniciado" + (r.dry_run ? " (simulado)" : " em modo REAL"), "sucesso");
  else toast(r.erro || "Não foi possível iniciar o bot.", "erro");
  await atualizarStatus();
}

/** Para o bot (SIGTERM -> saida limpa) */
async function pararBot() {
  el("btn-bot").disabled = true; // evita duplo-clique enquanto para
  const r = await pedirAcao("/api/bot/parar");
  if (r.ok) toast("Bot parado.", "sucesso");
  else toast(r.erro || "Não foi possível parar o bot.", "erro");
  await atualizarStatus();
}

// --- Botao principal Iniciar/Parar ---
el("btn-bot").addEventListener("click", () => {
  if (!estadoBot) return;
  if (estadoBot.a_correr) {
    pararBot();
  } else if (!estadoBot.dry_run_env) {
    // .env em modo REAL -> confirmacao extra explicita antes de arrancar
    abrirModal("modal-iniciar-real");
  } else {
    iniciarBot();
  }
});
el("btn-iniciar-real-sim").addEventListener("click", iniciarBot);

// --- Botao "Reiniciar bot" da barra de desfasamento ---
el("btn-reiniciar").addEventListener("click", async () => {
  el("btn-reiniciar").disabled = true;
  toast("A reiniciar o bot…");
  await pedirAcao("/api/bot/parar");
  const r = await pedirAcao("/api/bot/iniciar");
  if (r.ok) toast("Bot reiniciado no modo " + (r.dry_run ? "SIMULADO" : "REAL") + ".", "sucesso");
  else toast(r.erro || "Falha ao reiniciar.", "erro");
  el("btn-reiniciar").disabled = false;
  await atualizarStatus();
});

// --- Filtros da tabela de historico ---
// Ao mudar qualquer filtro, guardamos o valor e re-desenhamos o resumo
// (que redesenha a tabela de historico ja filtrada)
el("filtro-hist-simbolo").addEventListener("input", (e) => {
  filtrosHist.simbolo = e.target.value.trim();
  atualizarResumo();
});
el("filtro-hist-resultado").addEventListener("change", (e) => {
  filtrosHist.resultado = e.target.value;
  atualizarResumo();
});
el("filtro-hist-periodo").addEventListener("change", (e) => {
  filtrosHist.periodo = e.target.value;
  atualizarResumo();
});

// --- Filtro de chain (chips Todas / Solana / BSC) ---
// Delegacao: um listener trata os tres chips. Muda o filtro e re-desenha
// tudo de imediato (sem esperar pelo proximo polling).
document.getElementById("filtro-chain").addEventListener("click", (evento) => {
  const chip = evento.target.closest(".chip-chain");
  if (!chip) return;
  filtroChain = chip.dataset.chain;
  document.querySelectorAll(".chip-chain").forEach((c) => c.classList.remove("ativo"));
  chip.classList.add("ativo");
  atualizarTudo();
});

// --- Resetar bot (limpar dados de teste, so em modo SIMULADO) ---
el("btn-abrir-resetar").addEventListener("click", () => {
  el("input-resetar-confirmo").value = "";
  el("btn-resetar-confirmar").disabled = true;
  abrirModal("modal-resetar");
});
el("input-resetar-confirmo").addEventListener("input", () => {
  el("btn-resetar-confirmar").disabled = el("input-resetar-confirmo").value.trim() !== "RESETAR";
});
el("btn-resetar-confirmar").addEventListener("click", async () => {
  const r = await pedirAcao("/api/resetar", { confirmacao: el("input-resetar-confirmo").value.trim() });
  fecharModais();
  if (r.ok) toast(`Bot resetado — saldo reposto a $${r.saldo_inicial}.`, "sucesso");
  else toast(r.erro || "Não foi possível resetar.", "erro");
  atualizarTudo();
});

// --- Toggle da bonding curve (experimental, com confirmacao CONFIRMO) ---
async function atualizarCurva() {
  const dados = await (await fetch("/api/pumpfun")).json();
  el("chk-curva").checked = dados.ativo;
  el("aviso-curva").hidden = !dados.ativo;
}

el("chk-curva").addEventListener("click", (evento) => {
  // Ligar exige confirmacao explicita; desligar e livre e imediato
  if (evento.target.checked) {
    evento.preventDefault(); // so liga depois do CONFIRMO
    el("input-curva-confirmo").value = "";
    el("btn-curva-confirmar").disabled = true;
    abrirModal("modal-curva");
    el("input-curva-confirmo").focus();
  } else {
    pedirAcao("/api/pumpfun", { ativo: false }).then(() => {
      toast("Modo bonding curve desligado.", "sucesso");
      atualizarCurva();
    });
  }
});

el("input-curva-confirmo").addEventListener("input", () => {
  el("btn-curva-confirmar").disabled = el("input-curva-confirmo").value.trim() !== "CONFIRMO";
});
el("btn-curva-confirmar").addEventListener("click", async () => {
  const r = await pedirAcao("/api/pumpfun", {
    ativo: true, confirmacao: el("input-curva-confirmo").value.trim(),
  });
  fecharModais();
  if (r.ok) {
    toast("Modo bonding curve ATIVADO" + (r.precisa_reiniciar ? " — reinicia o bot para aplicar." : "."), "sucesso");
  } else {
    toast(r.erro || "Não foi possível ativar.", "erro");
  }
  atualizarCurva();
});

// --- Toggle do Modo Sniper Rapido (o mais arriscado dos 3 modos) ---
// Em modo REAL, pede a MESMA dupla confirmacao (2 modais) do toggle
// SIMULADO->REAL; em modo SIMULADO, um unico modal com CONFIRMO chega
// (dinheiro virtual - mas o backend exige CONFIRMO sempre, nos dois casos).
async function atualizarSniper() {
  const dados = await (await fetch("/api/sniper")).json();
  el("chk-sniper").checked = dados.ativo;
  el("aviso-sniper").hidden = !dados.ativo;
  el("sniper-limite-preview").textContent = "$" + dados.limite_diario_usd.toFixed(2);

  // Barra do gasto diario - so aparece com o modo ligado
  el("linha-sniper-orcamento").hidden = !dados.ativo;
  if (dados.ativo) {
    const pct = dados.limite_diario_usd > 0
      ? Math.min(100, (dados.gasto_hoje_usd / dados.limite_diario_usd) * 100) : 0;
    el("orcamento-sniper-barra").style.width = pct + "%";
    el("orcamento-sniper-barra").classList.toggle("cheio", pct >= 90);
    el("orcamento-sniper-texto").textContent =
      `$${dados.gasto_hoje_usd.toFixed(2)} / $${dados.limite_diario_usd.toFixed(2)} gastos hoje ` +
      `(restam $${dados.restante_hoje_usd.toFixed(2)})`;
  }

  // Aviso do trade-off do filtro de qualidade (momentum): so aparece
  // quando o modo esta ligado E ha pelo menos um sinal de momentum ativo
  const mostrarMomentum = dados.ativo && dados.momentum_ativo;
  el("linha-sniper-momentum").hidden = !mostrarMomentum;
  if (mostrarMomentum) {
    el("aviso-sniper-momentum").textContent =
      `⏱ Filtro de qualidade ativo: cada token pode demorar +${dados.momentum_janela_seg}s ` +
      `a decidir (espera por atividade real de compra/venda), mas entra em menos tokens fracos.`;
  }
}

el("chk-sniper").addEventListener("click", (evento) => {
  if (evento.target.checked) {
    evento.preventDefault(); // so liga depois da(s) confirmacao(oes)
    const emReal = estadoBot && !estadoBot.dry_run_env;
    if (emReal) {
      abrirModal("modal-sniper-aviso-real");  // 1o passo, so em REAL
    } else {
      el("input-sniper-confirmo").value = "";
      el("btn-sniper-confirmar").disabled = true;
      abrirModal("modal-sniper-confirmo");
      el("input-sniper-confirmo").focus();
    }
  } else {
    pedirAcao("/api/sniper", { ativo: false }).then(() => {
      toast("Modo Sniper Rápido desligado.", "sucesso");
      atualizarSniper();
    });
  }
});

// Modal H1 (so REAL) -> avanca para o modal de CONFIRMO (H2)
el("btn-sniper-real-continuar").addEventListener("click", () => {
  el("input-sniper-confirmo").value = "";
  el("btn-sniper-confirmar").disabled = true;
  abrirModal("modal-sniper-confirmo");
  el("input-sniper-confirmo").focus();
});

el("input-sniper-confirmo").addEventListener("input", () => {
  el("btn-sniper-confirmar").disabled = el("input-sniper-confirmo").value.trim() !== "CONFIRMO";
});
el("btn-sniper-confirmar").addEventListener("click", async () => {
  const r = await pedirAcao("/api/sniper", {
    ativo: true, confirmacao: el("input-sniper-confirmo").value.trim(),
  });
  fecharModais();
  if (r.ok) {
    toast("💀 Modo Sniper Rápido ATIVADO" + (r.precisa_reiniciar ? " — reinicia o bot para aplicar." : "."), "sucesso");
  } else {
    toast(r.erro || "Não foi possível ativar.", "erro");
  }
  atualizarSniper();
});

// --- Toggle do Copy Trading (replicar carteiras seguidas) ---
async function atualizarCopy() {
  const dados = await (await fetch("/api/copy")).json();
  el("chk-copy").checked = dados.ativo;
  el("aviso-copy").hidden = !dados.ativo;
  el("copy-carteiras-preview").textContent = dados.n_carteiras;
  el("copy-valor-preview").textContent = "$" + dados.valor_usd.toFixed(2);
  el("copy-limite-preview").textContent = "$" + dados.limite_diario_usd.toFixed(2);

  el("linha-copy-orcamento").hidden = !dados.ativo;
  if (dados.ativo) {
    const pct = dados.limite_diario_usd > 0
      ? Math.min(100, (dados.gasto_hoje_usd / dados.limite_diario_usd) * 100) : 0;
    el("orcamento-copy-barra").style.width = pct + "%";
    el("orcamento-copy-barra").classList.toggle("cheio", pct >= 90);
    el("orcamento-copy-texto").textContent =
      `$${dados.gasto_hoje_usd.toFixed(2)} / $${dados.limite_diario_usd.toFixed(2)} gastos hoje ` +
      `(restam $${dados.restante_hoje_usd.toFixed(2)})`;
  }
}

el("chk-copy").addEventListener("click", (evento) => {
  if (evento.target.checked) {
    evento.preventDefault(); // so liga depois do CONFIRMO
    el("input-copy-confirmo").value = "";
    el("btn-copy-confirmar").disabled = true;
    abrirModal("modal-copy-confirmo");
    el("input-copy-confirmo").focus();
  } else {
    pedirAcao("/api/copy", { ativo: false }).then(() => {
      toast("Copy Trading desligado.", "sucesso");
      atualizarCopy();
    });
  }
});

el("input-copy-confirmo").addEventListener("input", () => {
  el("btn-copy-confirmar").disabled = el("input-copy-confirmo").value.trim() !== "CONFIRMO";
});
el("btn-copy-confirmar").addEventListener("click", async () => {
  const r = await pedirAcao("/api/copy", {
    ativo: true, confirmacao: el("input-copy-confirmo").value.trim(),
  });
  fecharModais();
  if (r.ok) {
    toast("👥 Copy Trading ATIVADO" + (r.precisa_reiniciar ? " — reinicia o bot para aplicar." : "."), "sucesso");
  } else {
    toast(r.erro || "Não foi possível ativar.", "erro");
  }
  atualizarCopy();
});

// --- Toggle SIMULADO/REAL (controlo segmentado) ---

/** Pede ao backend para mudar o modo no .env */
async function mudarModo(dryRun, confirmacao = null) {
  fecharModais();
  const corpo = { dry_run: dryRun };
  if (confirmacao) corpo.confirmacao = confirmacao;
  const r = await pedirAcao("/api/modo", corpo);
  if (!r.ok) { toast(r.erro || "Não foi possível mudar o modo.", "erro"); return; }

  if (r.precisa_reiniciar) {
    toast("Modo alterado no .env — o bot precisa de reiniciar para aplicar.", "sucesso");
  } else {
    toast("Modo alterado para " + (dryRun ? "SIMULADO" : "REAL") + ".", "sucesso");
  }
  await atualizarStatus();
}

// SIMULADO: direcao segura, muda logo (sem dupla confirmacao)
el("btn-modo-simulado").addEventListener("click", () => {
  if (estadoBot && estadoBot.dry_run_env) return; // ja esta em simulado
  mudarModo(true);
});

// REAL: abre o 1.o modal de aviso; o resto da cadeia esta nos botoes abaixo
el("btn-modo-real").addEventListener("click", () => {
  if (estadoBot && !estadoBot.dry_run_env) return; // ja esta em real
  if (estadoBot && !estadoBot.fase2_ok) {
    toast("WALLET_PRIVATE_KEY não configurada — sem wallet não há modo REAL.", "erro");
    return;
  }
  abrirModal("modal-real-aviso");
});

// Modal A ("Tens a certeza?") -> passa ao modal B (escrever CONFIRMO)
el("btn-real-continuar").addEventListener("click", () => {
  abrirModal("modal-real-confirmo");
  el("input-confirmo").focus();
});

// Modal B: o botao final so ativa quando o texto for exatamente CONFIRMO
el("input-confirmo").addEventListener("input", () => {
  el("btn-real-confirmar").disabled = el("input-confirmo").value.trim() !== "CONFIRMO";
});
el("btn-real-confirmar").addEventListener("click", () => {
  mudarModo(false, el("input-confirmo").value.trim());
});

// ==========================================================================
// 5) Polling: buscar dados ao Flask e atualizar a pagina
// ==========================================================================

/** Atualiza o cabecalho: estado do bot, botao, seletor de modo e aviso */
async function atualizarStatus() {
  const resposta = await fetch("/api/bot/status");
  estadoBot = await resposta.json();

  // --- Indicador A CORRER / PARADO ---
  const bolinha = el("bolinha-bot");
  const estado = el("estado-bot");
  if (estadoBot.a_correr) {
    bolinha.classList.add("viva");
    estado.classList.add("a-correr");
    el("texto-estado-bot").textContent = "Bot: A CORRER";
  } else {
    bolinha.classList.remove("viva");
    estado.classList.remove("a-correr");
    el("texto-estado-bot").textContent = "Bot: PARADO";
  }

  // --- Botao Iniciar/Parar (cor e texto mudam com o estado) ---
  const btn = el("btn-bot");
  btn.disabled = false;
  if (estadoBot.a_correr) {
    btn.textContent = "Parar Bot";
    btn.className = "btn btn-perigo";
  } else {
    btn.textContent = "Iniciar Bot";
    btn.className = "btn btn-verde";
    // Sem Camada 1 nao vale a pena deixar arrancar (o backend tambem recusa)
    if (!estadoBot.camada1_ok) {
      btn.disabled = true;
      btn.title = "Configura DEEPSEEK_API_KEY no .env primeiro";
    }
  }

  // --- Multi-chain: badge da chain e filtro so aparecem com >1 rede ---
  const redes = estadoBot.redes_ativas || ["solana"];
  const badge = el("badge-chain");
  if (badge) {
    if (redes.length > 1) badge.textContent = "MULTI-CHAIN";
    else badge.textContent = redes[0] === "bsc" ? "BSC" : "SOLANA";
  }
  el("filtro-chain").hidden = redes.length <= 1;

  // --- Seletor SIMULADO/REAL reflete o .env ATUAL ---
  el("btn-modo-simulado").className =
    "segmento" + (estadoBot.dry_run_env ? " ativo-simulado" : "");
  el("btn-modo-real").className =
    "segmento" + (!estadoBot.dry_run_env ? " ativo-real" : "");

  // --- Aviso de desfasamento (bot a correr com modo != .env) ---
  el("aviso-desfasado").hidden = !estadoBot.desfasado;
  if (estadoBot.desfasado) {
    const modoBot = estadoBot.dry_run_bot ? "SIMULADO" : "REAL";
    const modoEnv = estadoBot.dry_run_env ? "SIMULADO" : "REAL";
    el("texto-desfasado").textContent =
      `O bot está a correr em modo ${modoBot}, mas o .env diz ${modoEnv} — reinicia para aplicar.`;
  }
}

/** Atualiza a seccao "Carteira do bot": endereco, saldo SOL e QR code.
    O QR e um SVG gerado pelo servidor (so o endereco publico, nunca a
    chave privada). So re-renderiza o QR quando o endereco muda. */
async function atualizarCarteira() {
  const resposta = await fetch("/api/wallet");
  const dados = await resposta.json();

  el("carteira-conteudo").hidden = !dados.configurada;
  el("vazio-carteira").hidden = dados.configurada;
  if (!dados.configurada) return;

  // Endereco + QR so mudam se a wallet mudar (evita flicker a cada polling)
  if (el("endereco-wallet").textContent !== dados.endereco) {
    el("endereco-wallet").textContent = dados.endereco;
    el("qr-wallet").innerHTML = dados.qr_svg || "";
  }
  el("saldo-wallet").textContent =
    dados.saldo_sol === null ? "N/A" : dados.saldo_sol.toFixed(4) + " SOL";

  // Wallet BSC (so aparece se configurada no .env)
  const bsc = dados.bsc;
  el("bloco-wallet-bsc").hidden = !bsc;
  if (bsc) {
    el("endereco-wallet-bsc").textContent = bsc.endereco;
    el("saldo-wallet-bsc").textContent =
      bsc.saldo_bnb === null || bsc.saldo_bnb === undefined
        ? "N/A" : bsc.saldo_bnb.toFixed(4) + " BNB";
  }
  // Se so a BSC estiver configurada, esconde a parte Solana (fica so a BSC)
  el("endereco-wallet").closest(".carteira").querySelectorAll(".qr-caixa, #endereco-wallet, #btn-copiar-endereco")
    .forEach((elem) => { elem.style.display = dados.so_bsc ? "none" : ""; });
}

// Botao "Copiar endereco": usa a API moderna do clipboard, com fallback
// (textarea + execCommand) para browsers/webviews mais antigos
async function copiarTexto(texto) {
  if (!texto || texto === "–") return;
  try {
    await navigator.clipboard.writeText(texto);
  } catch {
    const caixa = document.createElement("textarea");
    caixa.value = texto;
    document.body.appendChild(caixa);
    caixa.select();
    document.execCommand("copy");
    caixa.remove();
  }
  toast("Endereço copiado!", "sucesso");
}
el("btn-copiar-endereco").addEventListener("click",
  () => copiarTexto(el("endereco-wallet").textContent));
el("btn-copiar-endereco-bsc").addEventListener("click",
  () => copiarTexto(el("endereco-wallet-bsc").textContent));

/** Atualiza a secção "Tokens na carteira" (todos os SPL da wallet) */
async function atualizarTokensCarteira() {
  const dados = await (await fetch("/api/tokens_carteira")).json();
  const tokens = dados.tokens || [];

  el("vazio-tokens").hidden = tokens.length > 0;
  if (!dados.configurada) {
    el("vazio-tokens-msg").textContent = "Nenhuma wallet configurada — define WALLET_PRIVATE_KEY no .env.";
  } else if (!tokens.length) {
    el("vazio-tokens-msg").textContent = "A wallet não tem tokens SPL neste momento.";
  }

  el("tabela-tokens").innerHTML = tokens.map((t) => {
    // Abreviado na tabela (K/M/B/T); o valor exato completo fica no title
    const qtdExata = t.quantidade.toLocaleString("pt-PT", { maximumFractionDigits: 4 });
    const qtd = `<span title="${qtdExata}">${abreviarQuantidade(t.quantidade)}</span>`;
    const valor = t.valor_usd === null || t.valor_usd === undefined ? "–" : dinheiro(t.valor_usd);
    // Guardamos os dados do token nos data-* para o modal de envio os ler
    return `<tr>
      <td>${linkToken(t.mint, t.simbolo === "?" ? t.mint.slice(0, 4) + "…" : t.simbolo, "", "solana")}</td>
      <td>${qtd}</td>
      <td>${valor}</td>
      <td><button class="btn btn-mini btn-neutro" data-enviar-mint="${escHtml(t.mint)}"
                  data-enviar-simbolo="${escHtml(t.simbolo)}" data-enviar-saldo="${escHtml(t.quantidade)}">Enviar</button></td>
    </tr>`;
  }).join("");
}

// --- Modal de envio de token ---
let _envioToken = null; // {mint, simbolo, saldo}

function abrirModalEnvio(mint, simbolo, saldo) {
  _envioToken = { mint, simbolo, saldo };
  el("enviar-simbolo").textContent = simbolo === "?" ? "token" : simbolo;
  el("enviar-destino").value = "";
  el("enviar-quantidade").value = "";
  el("enviar-confirmo").value = "";
  el("enviar-saldo").textContent = `(tens ${Number(saldo).toLocaleString("pt-PT", { maximumFractionDigits: 4 })})`;
  // A caixa CONFIRMO so aparece em modo REAL (o backend exige na mesma)
  const emReal = estadoBot && !estadoBot.dry_run_env;
  el("enviar-confirmo-bloco").hidden = !emReal;
  abrirModal("modal-enviar");
}

el("btn-enviar-confirmar").addEventListener("click", async () => {
  if (!_envioToken) return;
  const destino = el("enviar-destino").value.trim();
  const quantidade = parseFloat(el("enviar-quantidade").value.replace(",", "."));
  if (!destino) { toast("Indica o endereço de destino.", "erro"); return; }
  if (!(quantidade > 0)) { toast("Indica uma quantidade válida.", "erro"); return; }

  const emReal = estadoBot && !estadoBot.dry_run_env;
  const corpo = { mint: _envioToken.mint, destino, quantidade };
  if (emReal) {
    const palavra = el("enviar-confirmo").value.trim();
    if (palavra !== "CONFIRMO") { toast("Escreve CONFIRMO para autorizar o envio real.", "erro"); return; }
    corpo.confirmacao = palavra;
  }
  const r = await pedirAcao("/api/enviar_token", corpo);
  fecharModais();
  toast(r.ok ? r.mensagem || "Envio efetuado." : r.erro || r.mensagem || "Falha no envio.",
        r.ok ? "sucesso" : "erro");
  atualizarTokensCarteira();
});

/** Atualiza a caixa "Log ao vivo" com as ultimas linhas do bot.log */
async function atualizarLog() {
  const resposta = await fetch("/api/bot/log");
  const dados = await resposta.json();
  const caixa = el("log-bot");

  const texto = dados.linhas.length
    ? dados.linhas.join("\n")
    : "O log aparece aqui quando o bot arrancar…";
  if (caixa.textContent === texto) return; // nada mudou

  // So faz scroll automatico se o utilizador ja estiver "colado" ao
  // fundo - se ele subiu para ler algo, nao o puxamos para baixo
  const coladoAoFundo =
    caixa.scrollHeight - caixa.scrollTop - caixa.clientHeight < 40;
  caixa.textContent = texto;
  if (coladoAoFundo) caixa.scrollTop = caixa.scrollHeight;
}

/** Atualiza cartoes, badge, graficos e tabela de historico */
async function atualizarResumo() {
  const resposta = await fetch("/api/resumo");
  const dados = await resposta.json();
  const c = dados.cartoes;

  // --- Cartoes de resumo (com flash quando o valor muda) ---
  definirValor("c-saldo-inicial", dinheiro(c.saldo_inicial));
  definirValor("c-saldo-livre", dinheiro(c.saldo_livre));
  definirValor("c-valor-posicoes", dinheiro(c.valor_posicoes));
  definirValor("c-valor-total", dinheiro(c.valor_total));
  definirValor("c-n-compras", String(c.n_compras));
  definirValor("c-n-vendas", String(c.n_vendas));
  definirValor("c-win-rate", c.win_rate === null ? "–" : c.win_rate + "%");

  const lucro = el("c-lucro-total");
  definirValor("c-lucro-total",
    `${dinheiroComSinal(c.lucro_total)} (${percentagemComSinal(c.lucro_total_pct)})`);
  pintarPorSinal(lucro, c.lucro_total);

  // --- Grafico da curva de saldo ---
  const curva = dados.curva_saldo;
  const temTrades = curva.length > 1; // o 1.o ponto e so o saldo inicial
  alternarVazio(graficoSaldo.canvas, "vazio-saldo", temTrades);
  graficoSaldo.data.labels = curva.map((p) => (p.timestamp ? dataHora(p.timestamp) : "início"));
  graficoSaldo.data.datasets[0].data = curva.map((p) => p.saldo);
  graficoSaldo.update();

  // --- Grafico de velas (ganhos vs perdas por venda) ---
  const gp = dados.ganhos_perdas;
  el("gp-ganho").textContent = dinheiro(gp.total_ganho);
  el("gp-perdido").textContent = dinheiro(gp.total_perdido);
  alternarVazio(graficoVelas.canvas, "vazio-velas", gp.velas.length > 0);
  graficoVelas.data.labels = gp.velas.map((v) => v.simbolo);
  graficoVelas.data.datasets[0].data = gp.velas.map((v) => [0, v.lucro_usd]); // barra de 0 ate ao lucro
  graficoVelas.data.datasets[0].backgroundColor = gp.velas.map(
    (v) => (v.lucro_usd >= 0 ? COR_VERDE : COR_VERMELHO)
  );
  // Datas das vendas para o tooltip (campo extra nosso, o Chart.js ignora-o)
  graficoVelas.data.datasets[0].datasVendas = gp.velas.map((v) => dataHora(v.timestamp));
  graficoVelas.update();

  // --- Tabela de historico (mais recente primeiro, ja vem ordenada) ---
  // Aplica o filtro de chain + os filtros proprios do historico
  const histVisivel = dados.historico.filter(passaFiltroChain).filter(passaFiltroHist).filter(passaFiltroModo);
  el("vazio-historico").hidden = histVisivel.length > 0;
  el("tabela-historico").innerHTML = histVisivel.map((h) => {
    const eVenda = h.tipo === "venda";
    // So as vendas tem lucro; nas compras a celula fica vazia
    let celulaLucro = "<td>–</td>";
    if (eVenda && h.lucro_usd !== undefined) {
      const classe = h.lucro_usd >= 0 ? "positivo" : "negativo";
      celulaLucro = `<td class="${classe}">${dinheiroComSinal(h.lucro_usd)}</td>`;
    }
    // Precos unitarios: nas compras so ha preco de compra; nas vendas
    // ha os dois (registos antigos sem estes campos mostram "–")
    const precoCompra = eVenda ? h.preco_compra_usd : h.preco_unitario_usd;

    const tagSniperHist = h.sniper_rapido ? ' <span class="tag sniper" title="Modo Sniper Rápido">💀</span>' : "";
    return `<tr>
      <td>${dataHora(h.timestamp)}</td>
      <td><span class="tag ${eVenda ? "venda" : "compra"}">${eVenda ? "VENDA" : "COMPRA"}</span></td>
      <td>${linkToken(h.mint, h.simbolo, "", h.chain)}${tagChain(h.chain)}${tagSniperHist}</td>
      <td>${dinheiro(h.valor_usd)}</td>
      <td>${precoUnitario(precoCompra)}</td>
      <td>${eVenda ? precoUnitario(h.preco_venda_usd) : "–"}</td>
      ${celulaLucro}
      <td><button class="btn-remover" data-remover-hist="${h.timestamp}" title="Remover do histórico">✕</button></td>
    </tr>`;
  }).join("");
}

/** Desenha uma das duas tabelas de desempenho (por modo ou por DEX).
    'grupos' vem de /api/estatisticas (lista de {chave,n_compras,n_vendas,
    win_rate,lucro_total}); 'formatarChave' transforma a chave em texto
    legivel (rotuloModo para modo; identidade para dex). */
function _desenharDesempenho(idTabela, idVazio, grupos, formatarChave) {
  el(idVazio).hidden = grupos.length > 0;
  el(idTabela).innerHTML = grupos.map((g) => {
    const classeLucro = g.lucro_total > 0 ? "positivo" : g.lucro_total < 0 ? "negativo" : "";
    return `<tr>
      <td>${escHtml(formatarChave(g.chave))}</td>
      <td>${g.n_compras}</td>
      <td>${g.n_vendas}</td>
      <td>${g.win_rate === null ? "–" : g.win_rate.toFixed(1) + "%"}</td>
      <td class="${classeLucro}">${dinheiroComSinal(g.lucro_total)}</td>
    </tr>`;
  }).join("");
}

/** Atualiza as duas seccoes de desempenho (por modo e por DEX/plataforma).
    So reflete a carteira SIMULADA - ver nota no dashboard.py. */
async function atualizarEstatisticas() {
  const dados = await (await fetch("/api/estatisticas")).json();
  _desenharDesempenho("tabela-desempenho-modo", "vazio-desempenho-modo", dados.por_modo, rotuloModo);
  _desenharDesempenho("tabela-desempenho-dex", "vazio-desempenho-dex", dados.por_dex, (dex) => dex);
}

/** Atualiza a tabela de posicoes abertas (com preco atual da Jupiter) */
// Guarda o ultimo valor_atual_usd visto por posicao (mint -> numero),
// para o flash verde/vermelho saber se o preco subiu ou desceu desde a
// ultima leitura - a tabela e sempre re-renderizada de raiz a cada
// polling, por isso precisamos de comparar com este estado a parte.
const _ultimoValorPosicao = {};

// Estado da ordenacao da tabela de posicoes (clicavel nos cabecalhos).
// Default: P/L nao realizado, do maior lucro para o maior prejuizo -
// as posicoes a dar lucro aparecem no topo, as que estao a perder ficam
// mais abaixo (pedido do utilizador).
const _ordenacaoPosicoes = { campo: "lucro_nao_realizado_usd", direcao: -1 };

/** Ordena a lista de posicoes pelo campo/direcao atuais. Valores null
    (ex: cotacao Jupiter falhou) vao sempre para o fim, em qualquer direcao -
    "sem dados" nao e nem o maior nem o menor valor, so falta de informacao. */
function _ordenarPosicoes(lista) {
  const { campo, direcao } = _ordenacaoPosicoes;
  return [...lista].sort((a, b) => {
    let va = a[campo], vb = b[campo];
    if (campo === "timestamp_compra") { va = va ? new Date(va).getTime() : null; vb = vb ? new Date(vb).getTime() : null; }
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    if (va < vb) return -1 * direcao;
    if (va > vb) return 1 * direcao;
    return 0;
  });
}

/** Atualiza as setas (▲/▼) nos cabecalhos clicaveis conforme a ordenacao atual */
function _atualizarSetasOrdenacao() {
  document.querySelectorAll("#tabela-posicoes-cabecalho .th-sortavel").forEach((th) => {
    const seta = th.querySelector(".seta-ordenacao");
    if (seta) seta.remove();
    if (th.dataset.sort === _ordenacaoPosicoes.campo) {
      th.insertAdjacentHTML("beforeend",
        `<span class="seta-ordenacao">${_ordenacaoPosicoes.direcao === -1 ? "▼" : "▲"}</span>`);
    }
  });
}

// Clique num cabecalho ordenavel: muda o campo (default desc) ou inverte
// a direcao se ja for o campo atual em uso.
document.querySelectorAll("#tabela-posicoes-cabecalho .th-sortavel").forEach((th) => {
  th.addEventListener("click", () => {
    const campo = th.dataset.sort;
    if (_ordenacaoPosicoes.campo === campo) {
      _ordenacaoPosicoes.direcao *= -1;
    } else {
      _ordenacaoPosicoes.campo = campo;
      _ordenacaoPosicoes.direcao = -1;  // novo campo: comeca do maior para o menor
    }
    atualizarPosicoes();
  });
});

async function atualizarPosicoes() {
  const resposta = await fetch("/api/posicoes");
  const dados = await resposta.json();
  const posicoes = dados.posicoes;

  const posVisiveis = _ordenarPosicoes(posicoes.filter(passaFiltroChain).filter(passaFiltroModo));
  el("vazio-posicoes").hidden = posVisiveis.length > 0;
  _atualizarSetasOrdenacao();
  el("tabela-posicoes").innerHTML = posVisiveis.map((p) => {
    // P/L nao realizado: pode ser null se a cotacao Jupiter falhou
    let celulaPL = "<td>N/A</td>";
    if (p.lucro_nao_realizado_usd !== null) {
      const classe = p.lucro_nao_realizado_usd >= 0 ? "positivo" : "negativo";
      celulaPL = `<td class="${classe}">${dinheiroComSinal(p.lucro_nao_realizado_usd)}</td>`;
    }

    // Flash verde/vermelho no VALOR ATUAL se mudou desde a ultima leitura
    // (a posicao esta "viva" - o utilizador ve-a mexer em tempo real)
    let classeFlashValor = "";
    if (p.valor_atual_usd !== null) {
      const anterior = _ultimoValorPosicao[p.mint];
      if (anterior !== undefined && anterior !== p.valor_atual_usd) {
        classeFlashValor = p.valor_atual_usd > anterior ? "flash-subida" : "flash-descida";
      }
      _ultimoValorPosicao[p.mint] = p.valor_atual_usd;
    }
    // Tag para distinguir compras na bonding curve das compras normais
    const tagCurva = p.origem === "bonding_curve"
      ? ' <span class="tag curva">BONDING CURVE</span>' : "";
    // 💀 Comprado pelo Modo Sniper Rapido (sem esperar pela IA)
    const tagSniper = p.sniper_rapido ? ' <span class="tag sniper" title="Comprado pelo Modo Sniper Rápido">💀 SNIPER</span>' : "";
    // 👥 Comprado por Copy Trading (replicou uma carteira seguida)
    const tagCopy = p.copy ? ' <span class="tag copy" title="Comprado por Copy Trading">👥 COPY</span>' : "";
    // Badge MANUAL: acompanhamento automatico desligado nesta posicao
    const auto = p.gestao_automatica !== false;
    const tagManual = auto ? "" : ' <span class="tag manual">MANUAL</span>';

    // Abreviado (K/M/B/T) com o valor exato no title (hover/tooltip)
    const qtdExata = p.quantidade_tokens.toLocaleString("pt-PT", { maximumFractionDigits: 0 });
    return `<tr>
      <td>${linkToken(p.mint, p.simbolo, p.dex, p.chain)}${tagChain(p.chain)}${tagCurva}${tagSniper}${tagCopy}${tagManual}</td>
      <td>${dinheiro(p.valor_investido_usd)}</td>
      <td>${precoUnitario(p.preco_compra_por_token_usd ?? p.preco_compra_usd)}</td>
      <td><span title="${qtdExata}">${abreviarQuantidade(p.quantidade_tokens)}</span></td>
      <td>${tempoDecorrido(p.timestamp_compra)}</td>
      <td class="${classeFlashValor}">${p.valor_atual_usd === null ? "N/A" : dinheiro(p.valor_atual_usd)}</td>
      ${celulaPL}
      <td class="acoes-posicao">
        <button class="btn btn-mini btn-perigo" data-vender="50" data-mint="${escHtml(p.mint)}" data-simbolo="${escHtml(p.simbolo)}">50%</button>
        <button class="btn btn-mini btn-perigo" data-vender="100" data-mint="${escHtml(p.mint)}" data-simbolo="${escHtml(p.simbolo)}">100%</button>
        <button class="btn btn-mini ${auto ? "btn-neutro" : "btn-verde"}"
                data-auto="${auto ? "0" : "1"}" data-mint="${escHtml(p.mint)}"
                title="${auto ? "Desativar acompanhamento automático" : "Reativar acompanhamento automático"}">
          ${auto ? "Desativar auto" : "Reativar auto"}
        </button>
      </td>
    </tr>`;
  }).join("");
}

/** Atualiza a watchlist: tokens fronteira a espera de decisao manual */
async function atualizarWatchlist() {
  const resposta = await fetch("/api/watchlist");
  const dados = await resposta.json();
  const lista = dados.watchlist;

  const wVisiveis = lista.filter(passaFiltroChain);
  el("vazio-watchlist").hidden = wVisiveis.length > 0;
  el("tabela-watchlist").innerHTML = wVisiveis.map((w) => {
    // Estado da reavaliacao periodica feita pelo bot
    let estado = '<span class="estado-liq">por avaliar</span>';
    if (w.reavaliacao) {
      estado = w.reavaliacao.liquidez_viva
        ? '<span class="estado-liq viva">● liquidez viva</span>'
        : '<span class="estado-liq morta">● sem rota (rug?)</span>';
    }
    const conf = w.confianca === null || w.confianca === undefined ? "–" : w.confianca + "%";
    const seguido = !!w.seguido;

    return `<tr>
      <td>${linkToken(w.mint, w.simbolo, w.dex, w.chain)}${tagChain(w.chain)}</td>
      <td><span class="pastilha ${w.score < 30 ? "score-baixo" : w.score <= 60 ? "score-medio" : "score-alto"}">${w.score}</span></td>
      <td>${conf}</td>
      <td>${dinheiro(w.liquidez_usd)}</td>
      <td>${marketcap(w.fdv_usd)}</td>
      <td>${estado}</td>
      <td>${tempoDecorrido(w.adicionado_em)}</td>
      <td>
        <button class="btn btn-mini ${seguido ? "btn-verde" : "btn-neutro"}"
                data-seguir="${seguido ? "0" : "1"}" data-mint="${escHtml(w.mint)}">
          ${seguido ? "★ A seguir" : "☆ Seguir"}
        </button>
        <button class="btn btn-mini btn-verde" data-comprar data-mint="${escHtml(w.mint)}" data-simbolo="${escHtml(w.simbolo)}">Comprar</button>
      </td>
    </tr>`;
  }).join("");
}

/** Atualiza o radar: TODOS os tokens que o bot detetou e analisou,
    comprados ou nao - uma janela para o que se passa no mercado */
async function atualizarRadar() {
  const resposta = await fetch("/api/radar");
  const dados = await resposta.json();
  const radar = dados.radar;

  const visiveis = radar.filter(passaFiltroChain).filter(passaFiltroModo);
  el("vazio-radar").hidden = visiveis.length > 0;
  el("tabela-radar").innerHTML = visiveis.map((r) => {
    // Cor do score: verde = seguro, amarelo = medio, vermelho = arriscado
    let classeScore = "score-alto";
    if (r.score < 30) classeScore = "score-baixo";
    else if (r.score <= 60) classeScore = "score-medio";

    // Tag COMPRADO junto ao simbolo (1.a coluna, sempre visivel no
    // telemovel - na ultima coluna ficava escondida pelo scroll)
    const tagComprado = r.comprado ? ' <span class="tag comprado">COMPRADO</span>' : "";

    return `<tr>
      <td>${linkToken(r.mint, r.simbolo, r.dex, r.chain)}${tagChain(r.chain)}${tagComprado}</td>
      <td><span class="pastilha ${classeScore}">${r.score}</span></td>
      <td>${dinheiro(r.liquidez_usd)}</td>
      <td>${marketcap(r.fdv_usd)}</td>
      <td>${escHtml(r.dex || "?")}</td>
      <td>${tempoDecorrido(r.timestamp)}</td>
    </tr>`;
  }).join("");
}

// ==========================================================================
// Botoes de trading nas tabelas (delegacao de eventos)
// ==========================================================================
// As tabelas sao re-renderizadas a cada polling, o que destroi botoes e
// os seus listeners. Solucao classica: UM listener no documento que
// apanha cliques nos botoes pelos atributos data-* (delegacao).
document.addEventListener("click", async (evento) => {
  // Preco truncado (numero minusculo demais para a tabela): clicar copia
  // o valor completo - cobre o telemovel, onde nao ha hover para o title
  const precoTrunc = evento.target.closest(".preco-truncado");
  if (precoTrunc) {
    copiarTexto(precoTrunc.dataset.copiar);
    return;
  }

  // Botao "Enviar" da seccao Tokens na carteira (abre o modal de envio)
  const btnEnviar = evento.target.closest("button[data-enviar-mint]");
  if (btnEnviar) {
    abrirModalEnvio(btnEnviar.dataset.enviarMint, btnEnviar.dataset.enviarSimbolo,
                    btnEnviar.dataset.enviarSaldo);
    return;
  }

  const btn = evento.target.closest(
    "button[data-vender], button[data-comprar], button[data-seguir], " +
    "button[data-auto], button[data-remover-hist]");
  if (!btn) return;
  const mint = btn.dataset.mint;
  const simbolo = btn.dataset.simbolo || "?";

  // --- Desativar/reativar acompanhamento automatico de uma posicao ---
  if (btn.dataset.auto !== undefined) {
    const ativar = btn.dataset.auto === "1";
    const r = await pedirAcao("/api/posicao/gestao", { mint, automatica: ativar });
    if (r.ok) toast(ativar ? "Acompanhamento automático reativado." : "Posição agora só manual (auto desligado).", "sucesso");
    else toast(r.erro || "Não foi possível alterar.", "erro");
    atualizarPosicoes();
    return;
  }

  // --- Remover uma entrada do historico (com confirmacao) ---
  if (btn.dataset.removerHist !== undefined) {
    if (!confirm("Remover esta entrada do histórico? Esta ação é irreversível (não afeta o saldo).")) return;
    const ts = encodeURIComponent(btn.dataset.removerHist);
    const resp = await fetch(`/api/historico/${ts}`, { method: "DELETE" });
    const r = await resp.json();
    if (r.ok) toast("Entrada removida do histórico.", "sucesso");
    else toast(r.erro || "Não foi possível remover.", "erro");
    atualizarResumo();
    return;
  }

  // --- Vender 50% / 100% de uma posicao ---
  if (btn.dataset.vender) {
    const pct = btn.dataset.vender;
    abrirModalAcao(
      `Vender ${pct}% de ${simbolo}`,
      `Vais vender ${pct}% da posição em ${simbolo} ao preço atual do mercado (via Jupiter).`,
      async (confirmacao) => {
        const corpo = { mint, percentagem: Number(pct) };
        if (confirmacao) corpo.confirmacao = confirmacao;
        const r = await pedirAcao("/api/vender", corpo);
        toast(r.ok ? r.mensagem || "Venda executada." : r.erro || "Falha na venda.",
              r.ok ? "sucesso" : "erro");
        atualizarTudo();
      }
    );
  }

  // --- Comprar um token da watchlist ---
  if (btn.hasAttribute("data-comprar")) {
    abrirModalAcao(
      `Comprar ${simbolo}`,
      `Vais comprar o valor configurado (MAX_TRADE_USD) de ${simbolo}. O token sai da watchlist e passa a posição aberta.`,
      async (confirmacao) => {
        const corpo = { mint, simbolo };
        if (confirmacao) corpo.confirmacao = confirmacao;
        const r = await pedirAcao("/api/comprar", corpo);
        toast(r.ok ? r.mensagem || "Compra executada." : r.erro || "Falha na compra.",
              r.ok ? "sucesso" : "erro");
        atualizarTudo();
      }
    );
  }

  // --- Seguir / deixar de seguir na watchlist (sem confirmacao: e inofensivo) ---
  if (btn.dataset.seguir !== undefined) {
    const r = await pedirAcao("/api/watchlist/seguir", {
      mint, seguir: btn.dataset.seguir === "1",
    });
    if (!r.ok) toast(r.erro || "Não foi possível atualizar.", "erro");
    atualizarWatchlist();
  }
});

/** Um ciclo completo de atualizacao. try/catch para que uma falha de
    rede momentanea nao mate o polling - tenta outra vez no proximo ciclo */
async function atualizarTudo() {
  try {
    // Tudo em paralelo (a de posicoes pode demorar por causa das
    // cotacoes Jupiter; assim nao atrasa os cartoes nem o estado)
    await Promise.all([
      atualizarResumo(),
      atualizarPosicoes(),
      atualizarWatchlist(),
      atualizarRadar(),
      atualizarStatus(),
      atualizarLog(),
      atualizarCarteira(),
      atualizarTokensCarteira(),
      atualizarCurva(),
      atualizarSniper(),
      atualizarCopy(),
      atualizarEstatisticas(),
    ]);
    el("ultima-atualizacao").textContent =
      "Atualizado às " + new Date().toLocaleTimeString("pt-PT");
    // Primeiros dados chegaram: tira os "esqueletos" de loading
    document.body.classList.remove("carregando");
  } catch (erro) {
    el("ultima-atualizacao").textContent =
      "Sem ligação ao servidor… a tentar de novo";
  }
}

// Arranque: atualiza ja, e depois repete a cada INTERVALO_POLLING_MS
atualizarTudo();
setInterval(atualizarTudo, INTERVALO_POLLING_MS);
