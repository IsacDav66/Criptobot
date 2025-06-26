import logging
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify, Blueprint 
import os
import json
import time
import psycopg2 
import psycopg2.extras
from datetime import datetime
import decimal
from dotenv import load_dotenv # <--- AÑADIR

load_dotenv()
app = Flask(__name__)
COMMAND_FILE = "web_command.txt"
CHART_DATA_FILE = "chart_data.json"
BOT_STATUS_FILE = "bot_status.json"
DATABASE_URL_WEB = os.environ.get("DATABASE_URL_BOT") 
BASE_ASSET_UI = "BTC" 
QUOTE_ASSET_UI = "USDT"
DEFAULT_CHECK_INTERVAL_FOR_TEMPLATE = 60 
HISTORY_LIMIT = 10

# Crear un Blueprint para todas nuestras rutas del bot con el prefijo /bot
bot_api = Blueprint('bot_api', __name__)

HTML_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
    <title>Control y Gráfico Bot Gemini</title>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body { font-family: sans-serif; margin: 0; padding:0; background-color: #131722; color: #d1d4dc; display: flex; flex-direction: column; height: 100vh; }
        .header-controls { padding: 10px; background-color: #1e222d; border-bottom: 1px solid #2a2e39; text-align: center;}
        .header-controls form { display: inline-block; margin-right: 5px;}
        .main-content { display: flex; flex-grow: 1; overflow: hidden;}
        .chart-container { flex-grow: 1; position: relative; min-width: 0; } 
        #tvchart { width: 100%; height: 100%; }
        .status-panel { width: 400px; min-width:380px; /* Un poco más ancho */ background-color: #1e222d; padding: 15px; overflow-y: auto; border-left: 1px solid #2a2e39; font-size: 0.85em;}
        .status-panel h2 { margin-top: 0; color: #efbb20; border-bottom: 1px solid #2a2e39; padding-bottom: 5px; margin-bottom: 10px;}
        .status-panel p { margin: 5px 0; word-wrap: break-word; line-height: 1.3;}
        .status-panel strong { color: #8c8f98; min-width: 140px; display: inline-block; }
        .status-panel span { color: #d1d4dc; }
        .pnl-positive { color: #26a69a !important; font-weight: bold; }
        .pnl-negative { color: #ef5350 !important; font-weight: bold; }
        .btn { padding: 8px 15px; margin: 5px; font-size: 14px; color: #fff; border: none; border-radius: 3px; cursor: pointer; }
        .btn-buy { background-color: #26a69a; } .btn-sell { background-color: #ef5350; }
        .btn-ia { background-color: #ff9800; } .btn-clear { background-color: #007bff; }
        .command-status, .history-log { margin-top: 15px; padding: 10px; background-color: #2a2e39; border-radius: 3px; border-top: 1px solid #3c404a;}
        hr.divider { border: none; border-top: 1px solid #2a2e39; margin: 10px 0; }
        .history-log h3 { margin-top: 0; color: #b0bec5; font-size: 1.1em; margin-bottom: 8px;}
        .history-log table { width: 100%; border-collapse: collapse; font-size: 0.9em; table-layout: fixed;}
        .history-log th, .history-log td { text-align: left; padding: 4px 3px; border-bottom: 1px solid #3c404a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .history-log th { color: #8c8f98; }
        .history-log tr:last-child td { border-bottom: none; }
        .col-time { width: 25%; } .col-action { width: 35%; } .col-price {width:15%; text-align:right!important;} .col-qty {width:15%; text-align:right!important;} .col-pnl {width:10%; text-align:right!important;}
    </style>
</head>
<body>
    <div class="header-controls">
        <form method="POST" action="{{ url_for('bot_api.send_command') }}"><input type="hidden" name="command" value="FORCE_BUY"><button type="submit" class="btn btn-buy">Forzar COMPRA</button></form>
        <form method="POST" action="{{ url_for('bot_api.send_command') }}"><input type="hidden" name="command" value="FORCE_SELL"><button type="submit" class="btn btn-sell">Forzar VENTA</button></form>
        <form method="POST" action="{{ url_for('bot_api.send_command') }}"><input type="hidden" name="command" value="FORCE_IA_CONSULT"><button type="submit" class="btn btn-ia">Forzar CONSULTA IA</button></form>
        <form method="POST" action="{{ url_for('bot_api.send_command') }}"><input type="hidden" name="command" value="CLEAR_FORCED_ACTION"><button type="submit" class="btn btn-clear">Limpiar Acción</button></form>
    </div>
    <div class="main-content">
        <div class="chart-container"><div id="tvchart"></div></div>
        <div class="status-panel">
            <h2>Estado del Bot</h2>
            <p><strong>Posición:</strong> <span id="status_has_position">N/A</span></p>
            <p><strong>Últ. Compra:</strong> <span id="status_last_buy_price">N/A</span></p>
            <p><strong>P&L USDT:</strong> <span id="status_pnl_usdt">N/A</span></p>
            <p><strong>P&L %:</strong> <span id="status_pnl_percent">N/A</span></p>
            <p><strong>Take Profit:</strong> <span id="status_tp_price">N/A</span></p>
            <p><strong>Stop Loss:</strong> <span id="status_sl_price">N/A</span></p>
            <p><strong>Tiempo en Posición:</strong> <span id="status_time_in_pos">N/A</span></p>
            <p><strong>Orden Abierta ID:</strong> <span id="status_open_order_id">N/A</span></p>
            <hr class="divider">
            <p><strong>Balance {{ BASE_ASSET }}:</strong> <span id="status_bal_base">N/A</span></p>
            <p><strong>Balance {{ QUOTE_ASSET }}:</strong> <span id="status_bal_quote">N/A</span></p>
            <hr class="divider">
            <p><strong>Última Acción Bot:</strong> <span id="status_last_action">N/A</span></p>
            <p><strong>Actualizado:</strong> <span id="status_timestamp">N/A</span></p>
            <p><strong>Próx. Ciclo en:</strong> <span id="status_next_cycle_countdown">Calculando...</span></p>
            <div class="command-status"><strong>Archivo Comando:</strong><br><span id="command_file_display">No hay comando.</span></div>
            <div class="history-log">
                <h3>Historial (Últimas {{ HISTORY_LIMIT }})</h3>
                <table>
                    <thead><tr><th class="col-time">Tiempo</th><th class="col-action">Acción</th><th class="col-price">Precio Ej.</th><th class="col-qty">Cant.</th><th class="col-pnl">P&L</th></tr></thead>
                    <tbody id="history_table_body"></tbody>
                </table>
            </div>
        </div>
    </div>
<script>
    let candlestickSeries = null; let chart = null; 
    let buyPriceLine = null; let tpPriceLine = null; let slPriceLine = null;
    const chartElementContainer = document.getElementById('tvchartContainer');
    const chartDiv = document.getElementById('tvchart');
    let countdownIntervalId = null;
    let currentCheckInterval = {{ DEFAULT_CHECK_INTERVAL }}; 
    let timeRemainingForNextCycle = currentCheckInterval;

    function formatTime(totalSeconds) {
        if (totalSeconds < 0) return "0s";
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${minutes > 0 ? minutes + 'm ' : ''}${seconds < 10 ? '0' : ''}${seconds}s`;
    }

    function startCycleCountdown() {
        if (countdownIntervalId) clearInterval(countdownIntervalId);
        timeRemainingForNextCycle = currentCheckInterval; 
        const countdownEl = document.getElementById('status_next_cycle_countdown');
        if (countdownEl) countdownEl.textContent = formatTime(timeRemainingForNextCycle);
        countdownIntervalId = setInterval(() => {
            timeRemainingForNextCycle--;
            if(countdownEl) countdownEl.textContent = timeRemainingForNextCycle < 0 ? "Esperando..." : formatTime(timeRemainingForNextCycle);
        }, 1000);
    }

    function updatePriceLine(existingLine, series, price, color, title) {
        if (existingLine && series) series.removePriceLine(existingLine); // Comprobar series
        if (price !== null && price !== undefined && series && typeof series.createPriceLine === 'function') {
            return series.createPriceLine({ price: parseFloat(price), color: color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: title });
        } return null;
    }
    
    function updateChartData(ohlcData) {
        if (!candlestickSeries) { /* console.warn("updateChartData: candlestickSeries no inicializada."); */ return; }
        if (ohlcData && ohlcData.length > 0) {
            const formattedOhlcData = ohlcData.map(d => ({
                time: d.time, open: parseFloat(d.open), high: parseFloat(d.high),
                low: parseFloat(d.low), close: parseFloat(d.close)
            }));
            candlestickSeries.setData(formattedOhlcData);
        } else {
            candlestickSeries.setData([]);
        }
    }

    function updateStatusPanel(statusData) {
        if (!statusData) return;
        const na = 'N/A';
        document.getElementById('status_has_position').textContent = statusData.has_position ? 'Sí' : 'No';
        document.getElementById('status_last_buy_price').textContent = statusData.last_buy_price ? parseFloat(statusData.last_buy_price).toFixed(2) : na;
        const pnlUsdtEl = document.getElementById('status_pnl_usdt');
        pnlUsdtEl.textContent = (statusData.pnl_usdt !== null && statusData.pnl_usdt !== undefined) ? parseFloat(statusData.pnl_usdt).toFixed(2) : na;
        pnlUsdtEl.className = statusData.pnl_usdt > 0 ? 'pnl-positive' : (statusData.pnl_usdt < 0 ? 'pnl-negative' : '');
        const pnlPercentEl = document.getElementById('status_pnl_percent');
        pnlPercentEl.textContent = (statusData.pnl_percent !== null && statusData.pnl_percent !== undefined) ? parseFloat(statusData.pnl_percent).toFixed(2) + '%' : na;
        pnlPercentEl.className = statusData.pnl_percent > 0 ? 'pnl-positive' : (statusData.pnl_percent < 0 ? 'pnl-negative' : '');
        document.getElementById('status_tp_price').textContent = statusData.target_profit_price ? parseFloat(statusData.target_profit_price).toFixed(2) : na;
        document.getElementById('status_sl_price').textContent = statusData.stop_loss_price ? parseFloat(statusData.stop_loss_price).toFixed(2) : na;
        document.getElementById('status_time_in_pos').textContent = statusData.time_in_position || na;
        document.getElementById('status_open_order_id').textContent = statusData.open_order_id || na;
        document.getElementById('status_bal_base').textContent = (statusData.base_asset_balance !== null && statusData.base_asset_balance !== undefined) ? parseFloat(statusData.base_asset_balance).toFixed(8) : na;
        document.getElementById('status_bal_quote').textContent = (statusData.quote_asset_balance !== null && statusData.quote_asset_balance !== undefined) ? parseFloat(statusData.quote_asset_balance).toFixed(4) : na;
        document.getElementById('status_last_action').textContent = statusData.last_bot_action || na;
        document.getElementById('status_timestamp').textContent = statusData.timestamp ? new Date(statusData.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : na;
        if (statusData.check_interval_seconds && parseInt(statusData.check_interval_seconds, 10) > 0) {
            currentCheckInterval = parseInt(statusData.check_interval_seconds, 10);
        }
        startCycleCountdown(); 
        if (!candlestickSeries) return; 
        if (statusData.has_position && statusData.last_buy_price) {
            buyPriceLine = updatePriceLine(buyPriceLine, candlestickSeries, statusData.last_buy_price, '#3498db', 'Compra');
            tpPriceLine = updatePriceLine(tpPriceLine, candlestickSeries, statusData.target_profit_price, '#2ecc71', 'TP');
            slPriceLine = updatePriceLine(slPriceLine, candlestickSeries, statusData.stop_loss_price, '#e74c3c', 'SL');
        } else {
            if (buyPriceLine && candlestickSeries) candlestickSeries.removePriceLine(buyPriceLine); buyPriceLine = null;
            if (tpPriceLine && candlestickSeries) candlestickSeries.removePriceLine(tpPriceLine); tpPriceLine = null;
            if (slPriceLine && candlestickSeries) candlestickSeries.removePriceLine(slPriceLine); slPriceLine = null;
        }
    }

    function updateHistoryTable(historyData) {
        const tableBody = document.getElementById('history_table_body');
        if (!tableBody) { console.error("Elemento #history_table_body no encontrado."); return; }
        tableBody.innerHTML = ''; 
        // console.log("updateHistoryTable - Datos recibidos:", JSON.stringify(historyData, null, 2)); // DESCOMENTAR PARA DEPURAR DATOS CRUDOS

        if (historyData && historyData.length > 0) {
            historyData.forEach((log, index) => {
                // console.log(`Historial #${index}: `, log); // DESCOMENTAR PARA DEPURAR CADA LOG
                const row = tableBody.insertRow();
                const naDisplay = '-';

                row.insertCell().textContent = log.timestamp ? new Date(log.timestamp).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second:'2-digit'}) : naDisplay;
                row.insertCell().textContent = log.accion_bot || naDisplay;
                row.insertCell().textContent = (log.precio_ejecutado !== null) ? parseFloat(log.precio_ejecutado).toFixed(2) : naDisplay;
                row.insertCell().textContent = (log.cantidad_base_ejecutada !== null) ? parseFloat(log.cantidad_base_ejecutada).toFixed(5) : naDisplay;
                
                let pnlText = naDisplay; let pnlClass = '';
                if (log.ganancia_perdida_operacion_usdt !== null) {
                    const pnl = parseFloat(log.ganancia_perdida_operacion_usdt);
                    pnlText = pnl.toFixed(2);
                    if (pnl > 0) pnlClass = 'pnl-positive'; else if (pnl < 0) pnlClass = 'pnl-negative';
                }
                const cellPnl = row.insertCell();
                cellPnl.textContent = pnlText; if (pnlClass) cellPnl.className = pnlClass;
            });
        } else {
            // console.log("updateHistoryTable: No hay datos de historial para mostrar.");
            const row = tableBody.insertRow();
            const cell = row.insertCell();
            cell.colSpan = 5; 
            cell.textContent = 'No hay historial reciente.';
            cell.style.textAlign = 'center';
        }
    }
    
    function handleResize() { if (chart && chartDiv) chart.resize(chartDiv.clientWidth, chartDiv.clientHeight); }

    document.addEventListener('DOMContentLoaded', function() {
        console.log("DOM Loaded. LightweightCharts type:", typeof LightweightCharts);
        if (typeof LightweightCharts === 'undefined' || LightweightCharts === null || typeof LightweightCharts.createChart !== 'function') {
            console.error("LightweightCharts o LightweightCharts.createChart no está definido."); return;
        }

        const chartDiv = document.getElementById('tvchart'); // El div interno para el gráfico
        if (!chartDiv) {
            console.error("Elemento #tvchart no encontrado.");
            return;
        }
        console.log("Elemento #tvchart encontrado:", chartDiv);

        try {
            chart = LightweightCharts.createChart(chartDiv, { // Pasar el div específico
                width: chartDiv.clientWidth, // Usar clientWidth/Height del div del gráfico
                height: chartDiv.clientHeight,
                layout: { background: { type: 'solid', color: '#131722' }, textColor: '#d1d4dc' },
                grid: { vertLines: { color: '#2a2e39' }, horzLines: { color: '#2a2e39' }},
                timeScale: { timeVisible: true, secondsVisible: false },
            });
            console.log("Objeto Chart creado:", chart);
        } catch (e) {
            console.error("Error durante LightweightCharts.createChart:", e);
            return;
        }

        // Intento 1: Método directo (que fallaba)
        if (chart && typeof chart.addCandlestickSeries === 'function') {
            console.log("chart.addCandlestickSeries (directo) ES una función.");
            candlestickSeries = chart.addCandlestickSeries({ upColor: '#26a69a', downColor: '#ef5350' });
        } 
        // Intento 2: Usando chart.addSeries y LightweightCharts.CandlestickSeries (conjetura)
        else if (chart && typeof chart.addSeries === 'function' && LightweightCharts.CandlestickSeries) {
            console.log("chart.addSeries ES una función. LightweightCharts.CandlestickSeries existe. Intentando...");
            try {
                candlestickSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
                    upColor: '#26a69a', downColor: '#ef5350',
                    borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350'
                });
                console.log("Serie de velas creada con addSeries(LightweightCharts.CandlestickSeries):", candlestickSeries);
            } catch (e) {
                console.error("Error usando chart.addSeries(LightweightCharts.CandlestickSeries):", e);
                candlestickSeries = null; // Asegurar que es nulo si falla
            }
        }
        // Intento 3: Si los anteriores fallan, listar propiedades.
        else {
            console.error("Ni chart.addCandlestickSeries ni chart.addSeries(LightweightCharts.CandlestickSeries) funcionaron como se esperaba.");
            if(chart){
                console.log("Inspeccionando el objeto 'chart':");
                for (const prop in chart) {
                    if (Object.prototype.hasOwnProperty.call(chart, prop)) {
                        console.log(`  Propiedad: ${prop}, Tipo: ${typeof chart[prop]}`);
                    }
                }
            }
        }

        if (candlestickSeries) {
            console.log("Serie de velas (candlestickSeries) parece estar inicializada:", candlestickSeries);
            // Añadir datos de ejemplo para ver si el gráfico se renderiza
            candlestickSeries.setData([
                { time: Math.floor(Date.now() / 1000) - 86400 * 2, open: 16000, high: 16100, low: 15950, close: 16050 }, 
                { time: Math.floor(Date.now() / 1000) - 86400, open: 16050, high: 16150, low: 16000, close: 16100 }, 
                { time: Math.floor(Date.now() / 1000), open: 16100, high: 16200, low: 16080, close: 16180 }, 
            ]);
            console.log("Datos de ejemplo añadidos a la serie.");
        } else {
            console.warn("candlestickSeries no se pudo inicializar.");
        }
        
        window.addEventListener('resize', handleResize);
        handleResize(); 

        fetch("{{ url_for('bot_api.get_initial_data') }}")
            .then(response => response.json())
            .then(data => {
                console.log("Initial data fetched:", data); // LOG PARA VER DATOS INICIALES
                if (data.chart_data) { updateChartData(data.chart_data); }
                if (data.bot_status) { updateStatusPanel(data.bot_status); } 
                else { startCycleCountdown(); }
                if (data.history_data) { updateHistoryTable(data.history_data); } // Cargar historial
                if (chart) handleResize();
            })
            .catch(err => { console.error("Error fetching initial data:", err); startCycleCountdown(); });

        const sseCombined = new EventSource("{{ url_for('bot_api.stream_all_data') }}");
        sseCombined.onmessage = function(event) {
            try {
                const combinedData = JSON.parse(event.data);
                // console.log("SSE data received:", combinedData); // LOG PARA VER DATOS SSE
                if (combinedData.chart_data) { updateChartData(combinedData.chart_data); }
                if (combinedData.bot_status) { updateStatusPanel(combinedData.bot_status); }
                if (combinedData.history_data) { updateHistoryTable(combinedData.history_data); }
                document.getElementById('command_file_display').textContent = combinedData.command_file_status || "No hay comando.";
            } catch (e) { console.error("Error parsing SSE data:", e, "Received data:", event.data); }
        };
        sseCombined.onerror = function(err) { console.error("SSE Combined failed:", err); };
    });
</script>
</body>
</html>
"""


# --- Funciones Helper ---

def get_db_connection_web():
    if not DATABASE_URL_WEB: 
        logging.warning("DATABASE_URL_WEB no configurada.")
        return None
    try: 
        return psycopg2.connect(DATABASE_URL_WEB)
    except Exception as e: 
        logging.error(f"Error conectando a BD desde la web: {e}")
        return None

def fetch_history_from_db(limit=HISTORY_LIMIT):
    history = []
    conn = get_db_connection_web()
    if conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(f"""
                    SELECT 
                        timestamp, accion_bot, simbolo, 
                        precio_ejecutado, cantidad_base_ejecutada, costo_total_usdt, 
                        tipo_orden_ia, ganancia_perdida_operacion_usdt, orderid_abierta
                    FROM trading_log 
                    ORDER BY id DESC 
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()
                for row_data in rows:
                    processed_row = {}
                    for key, value in dict(row_data).items():
                        if isinstance(value, decimal.Decimal):
                            processed_row[key] = float(value)
                        elif isinstance(value, datetime):
                             processed_row[key] = value.isoformat()
                        else:
                            processed_row[key] = value
                    history.append(processed_row)
        except Exception as e:
            logging.error(f"Error obteniendo historial de BD: {e}")
        finally:
            conn.close()
    return history

def write_command(command_str):
    try:
        with open(COMMAND_FILE, 'w') as f: f.write(command_str.strip().upper())
        logging.info(f"Comando '{command_str}' escrito en {COMMAND_FILE}")
    except Exception as e: logging.error(f"Error escribiendo comando en {COMMAND_FILE}: {e}")

def get_command_file_status():
    if os.path.exists(COMMAND_FILE):
        try:
            with open(COMMAND_FILE, 'r') as f: return f.read().strip()
        except Exception as e: logging.error(f"Error leyendo {COMMAND_FILE}: {e}"); return "Error."
    return None

def read_json_file(filepath, default_value):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                content = f.read()
                if not content: return default_value
                return json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            logging.warning(f"Error leyendo o parseando {filepath}: {e}")
            return default_value
    return default_value

# --- Rutas de la Aplicación (usando el Blueprint) ---

@bot_api.route('/', methods=['GET'])
def index():
    # El HTML_TEMPLATE debe estar definido fuera de esta función, globalmente o importado.
    return render_template_string(HTML_TEMPLATE, 
                                  COMMAND_FILE=COMMAND_FILE,
                                  BASE_ASSET=BASE_ASSET_UI,
                                  QUOTE_ASSET=QUOTE_ASSET_UI,
                                  DEFAULT_CHECK_INTERVAL=DEFAULT_CHECK_INTERVAL_FOR_TEMPLATE,
                                  HISTORY_LIMIT=HISTORY_LIMIT)

@bot_api.route('/command', methods=['POST'])
def send_command():
    command_from_form = request.form.get('command')
    if command_from_form: write_command(command_from_form)
    # Redirigir a la página principal del blueprint
    return redirect(url_for('bot_api.index'))

@bot_api.route('/get_initial_data')
def get_initial_data():
    chart_data = read_json_file(CHART_DATA_FILE, [])
    bot_status = read_json_file(BOT_STATUS_FILE, {})
    history_data = fetch_history_from_db() 
    logging.info(f"Initial data request: {len(history_data)} history items")
    return jsonify({
        "chart_data": chart_data, 
        "bot_status": bot_status,
        "history_data": history_data 
    })

@bot_api.route('/stream_all_data')
def stream_all_data():
    def event_stream():
        last_chart_data_str = ""; last_bot_status_str = ""; last_command_file_status = ""; last_history_str = ""
        while True:
            chart_data = read_json_file(CHART_DATA_FILE, [])
            bot_status = read_json_file(BOT_STATUS_FILE, {})
            command_status = get_command_file_status() 
            history_data = fetch_history_from_db() 

            current_chart_data_str = json.dumps(chart_data if chart_data else [])
            current_bot_status_str = json.dumps(bot_status if bot_status else {})
            current_command_status_str = command_status or "No hay comando."
            current_history_str = json.dumps(history_data if history_data else [])

            if (current_chart_data_str != last_chart_data_str or
                current_bot_status_str != last_bot_status_str or
                current_command_status_str != last_command_file_status or
                current_history_str != last_history_str ): 
                
                payload = {
                    "chart_data": chart_data, "bot_status": bot_status,
                    "command_file_status": current_command_status_str,
                    "history_data": history_data 
                }
                yield f"data: {json.dumps(payload)}\n\n" 
                
                last_chart_data_str = current_chart_data_str
                last_bot_status_str = current_bot_status_str
                last_command_file_status = current_command_status_str
                last_history_str = current_history_str
            
            time.sleep(2)
    return Response(event_stream(), mimetype="text/event-stream")

# --- Registro del Blueprint y Ejecución ---

# Registrar el Blueprint en la aplicación principal de Flask
app.register_blueprint(bot_api)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - WebApp - %(message)s')
    if not DATABASE_URL_WEB:
        logging.error("DATABASE_URL_BOT (usada como DATABASE_URL_WEB) no está configurada. El historial no funcionará.")
    
    # Escuchar solo localmente para que Nginx actúe como proxy inverso
    # Si quieres seguir accediendo por IP:5000 directamente, mantenlo en '0.0.0.0'
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)