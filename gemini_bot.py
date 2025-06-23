# --- START OF FILE gemini_bot.py ---

import os
import time
import logging
import decimal
from datetime import datetime, timedelta
import json # Para guardar datos del gráfico y estado

# --- BEGIN WORKAROUND for pandas_ta and NumPy 2.0+ ---
import numpy as np
if not hasattr(np, 'NaN'):
    setattr(np, 'NaN', np.nan)
# --- END WORKAROUND ---

import google.generativeai as genai
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
import pandas as pd
import pandas_ta as ta

load_dotenv()

# --- Configuración General ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuración de Base de Datos PostgreSQL ---
DATABASE_URL = os.environ.get("DATABASE_URL_BOT")
DB_TABLE_NAME = 'trading_log'
DB_COLUMNS_TYPES = {
    "timestamp": "TIMESTAMP WITH TIME ZONE", "accion_bot": "VARCHAR(50)", "simbolo": "VARCHAR(15)",
    "precio_ejecutado": "DECIMAL(20, 8)", "cantidad_base_ejecutada": "DECIMAL(20, 8)",
    "costo_total_usdt": "DECIMAL(20, 8)", "tipo_orden_ia": "VARCHAR(20)",
    "respuesta_ia_completa": "TEXT", "tiene_posicion_despues": "BOOLEAN",
    "precio_ultima_compra_despues": "DECIMAL(20, 8)", "balance_base_despues": "DECIMAL(20, 8)",
    "balance_quote_despues": "DECIMAL(20, 8)", "ganancia_perdida_operacion_usdt": "DECIMAL(20, 8)",
    "orderid_abierta": "VARCHAR(50)", "notas_adicionales": "TEXT"
}
DB_COLUMN_ORDER = list(DB_COLUMNS_TYPES.keys())

# --- Configuración de IA de Google Gemini ---
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")
model_ai = None
if GOOGLE_AI_API_KEY:
    genai.configure(api_key=GOOGLE_AI_API_KEY)
    AI_MODEL_NAME = 'gemini-1.5-flash-latest'
    try:
        model_ai = genai.GenerativeModel(AI_MODEL_NAME)
        logging.info(f"Modelo IA Gemini '{AI_MODEL_NAME}' cargado.")
    except Exception as e:
        logging.error(f"Error cargando modelo IA '{AI_MODEL_NAME}': {e}")
else:
    logging.warning("GOOGLE_AI_API_KEY no encontrada. IA usará simulación.")

# --- Configuración de Binance ---
USE_TESTNET = True
if USE_TESTNET:
    BINANCE_API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY")
    BINANCE_API_SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET")
    logging.info("--- USANDO BINANCE TESTNET ---")
else:
    BINANCE_API_KEY = os.environ.get("BINANCE_PROD_API_KEY")
    BINANCE_API_SECRET = os.environ.get("BINANCE_PROD_API_SECRET")
    logging.info("--- USANDO BINANCE PRODUCCIÓN ---")

if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    logging.error("Claves API Binance no encontradas. Saliendo.")
    exit()
try:
    binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=USE_TESTNET)
    binance_client.ping()
    logging.info(f"Conexión a Binance {'Testnet' if USE_TESTNET else 'Producción'} OK.")
    server_time_offset = int(binance_client.get_server_time()['serverTime']) - int(time.time() * 1000)
    logging.info(f"Desfase horario Binance: {server_time_offset} ms")
except Exception as e:
    logging.error(f"Error conectando a Binance: {e}")
    exit()

# --- Parámetros de Trading ---
SYMBOL_EXCHANGE = 'BTCUSDT'
BASE_ASSET = 'BTC'
QUOTE_ASSET = 'USDT'
ORDER_AMOUNT_BASE = decimal.Decimal('0.0002')
CHECK_INTERVAL = 60 # Reducir para actualizaciones más frecuentes del gráfico y estado si se desea
TARGET_PROFIT_PERCENT = decimal.Decimal('2.0')
STOP_LOSS_PERCENT = decimal.Decimal('1.0')
ORDER_TIMEOUT_MINUTES = 15
KLINE_INTERVAL_FOR_INDICATORS = Client.KLINE_INTERVAL_15MINUTE # O un intervalo más corto para el gráfico, ej. 1m
KLINE_LIMIT_FOR_INDICATORS = 100 # Para indicadores
KLINE_LIMIT_FOR_CHART = 200 # Velas para el gráfico, puede ser mayor

COMMAND_FILE = "web_command.txt"
CHART_DATA_FILE = "chart_data.json"
BOT_STATUS_FILE = "bot_status.json"
MAX_CHART_POINTS = KLINE_LIMIT_FOR_CHART # Cuántas velas enviar al gráfico

# --- Estado del Bot ---
has_position = False
last_buy_price = decimal.Decimal('0.0')
entry_timestamp = None # Para calcular tiempo en posición
symbol_info_cache = {}
open_order_details = None
current_forced_action = None

# --- Funciones de Base de Datos PostgreSQL ---
def get_db_connection():
    if not DATABASE_URL: logging.error("DATABASE_URL no configurada."); return None
    try: return psycopg2.connect(DATABASE_URL)
    except Exception as e: logging.error(f"Error conectando a BD: {e}"); return None

def initialize_db_table():
    conn = get_db_connection();
    if not conn: return
    cols_sql = ",\n".join([f'"{cn}" {ct}' for cn,ct in DB_COLUMNS_TYPES.items()])
    create_sql = f"CREATE TABLE IF NOT EXISTS {DB_TABLE_NAME} (id SERIAL PRIMARY KEY, {cols_sql});"
    try:
        with conn.cursor() as cur: cur.execute(create_sql)
        conn.commit(); logging.info(f"Tabla '{DB_TABLE_NAME}' verificada/creada.")
    except Exception as e: logging.error(f"Error creando/verificando tabla: {e}")
    finally: conn.close() if conn else None

def log_to_db(data_dict):
    conn=get_db_connection();
    if not conn: return
    vals=[];
    for col_name in DB_COLUMN_ORDER:
        val=data_dict.get(col_name)
        if isinstance(val,decimal.Decimal): vals.append(val)
        elif val is None or val=="N/A" or str(val).strip()=="": vals.append(None if DB_COLUMNS_TYPES.get(col_name,"").startswith("DECIMAL") or DB_COLUMNS_TYPES.get(col_name)=="BOOLEAN" else (str(val) if val is not None else None))
        elif isinstance(val,bool): vals.append(val)
        else: vals.append(str(val))
    cols_str=",".join([f'"{c}"' for c in DB_COLUMN_ORDER]); ph_str=",".join(["%s"]*len(DB_COLUMN_ORDER))
    ins_sql=f"INSERT INTO {DB_TABLE_NAME} ({cols_str}) VALUES ({ph_str});"
    try:
        with conn.cursor() as cur: cur.execute(ins_sql,tuple(vals))
        conn.commit(); logging.debug(f"Datos insertados en '{DB_TABLE_NAME}'.")
    except Exception as e: logging.error(f"Error insertando en BD: {e}"); logging.error(f"Datos: {dict(zip(DB_COLUMN_ORDER,vals))}")
    finally: conn.close() if conn else None

# --- Funciones Binance (Auxiliares, Interacción) ---
def get_binance_symbol_info(symbol):
    if symbol not in symbol_info_cache:
        try: info = binance_client.get_symbol_info(symbol); symbol_info_cache[symbol] = info
        except BinanceAPIException as e: logging.error(f"Error API info {symbol}: {e}"); return None
        logging.info(f"Info y filtros para {symbol} cacheados.")
    return symbol_info_cache[symbol]

def _get_filter_value(si, ft, fk):
    if not si or 'filters' not in si: return None
    for f in si['filters']:
        if f['filterType']==ft: return decimal.Decimal(str(f.get(fk,'0')))
    return None

def format_quantity(si, q):
    ss=_get_filter_value(si,'LOT_SIZE','stepSize'); 
    q_decimal = decimal.Decimal(str(q))
    return (q_decimal // ss) * ss if ss and ss > 0 else q_decimal

def format_price(si, p):
    ts=_get_filter_value(si,'PRICE_FILTER','tickSize'); 
    p_decimal = decimal.Decimal(str(p))
    return (p_decimal // ts) * ts if ts and ts > 0 else p_decimal

def check_min_notional(si, q, p):
    mn_val=_get_filter_value(si,'NOTIONAL','minNotional')
    if mn_val is None: logging.warning(f"No minNotional para {si.get('symbol','N/A')}. Asume OK."); return True
    q_decimal = decimal.Decimal(str(q))
    p_decimal = decimal.Decimal(str(p))
    cur_not = q_decimal * p_decimal
    if cur_not < mn_val: logging.warning(f"Orden no cumple MIN_NOTIONAL ({mn_val}). Nocional: {cur_not}"); return False
    logging.debug(f"MIN_NOTIONAL: {cur_not} >= {mn_val}. OK."); return True

def get_binance_asset_balance(asset):
    try: bal_info=binance_client.get_asset_balance(asset=asset); return decimal.Decimal(bal_info['free']) if bal_info and 'free' in bal_info else decimal.Decimal('0.0')
    except Exception as e: logging.error(f"Error balance {asset}: {e}"); return decimal.Decimal('0.0')

# --- Funciones para Datos Adicionales e Indicadores ---
def obtener_klines_df(symbol, interval, limit=100):
    try:
        klines = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        if not klines: logging.warning(f"No se recibieron klines para {symbol}"); return None
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                                           'close_time', 'quote_asset_volume', 'number_of_trades', 
                                           'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
        for col in ['open', 'high', 'low', 'close', 'volume']: df[col] = pd.to_numeric(df[col])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e: logging.error(f"Error obteniendo/procesando klines {symbol}: {e}"); return None

def calcular_indicadores(df):
    if df is None or df.empty or len(df) < 20: # Ajustar si los indicadores necesitan más
        logging.warning("DataFrame vacío o insuficiente para calcular indicadores.")
        return df
    try:
        df.ta.rsi(length=14, append=True, col_names=('RSI_14',))
        df.ta.sma(length=20, append=True, col_names=('SMA_20',))
        df.ta.sma(length=50, append=True, col_names=('SMA_50',))
        return df
    except Exception as e: logging.error(f"Error calculando indicadores: {e}"); return df

def formatear_klines_para_prompt(df, num_klines_to_show=5):
    if df is None or df.empty or len(df) < num_klines_to_show: return "Datos de klines insuficientes."
    relevant_klines = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].tail(num_klines_to_show)
    summary_str = f"Ultimas {num_klines_to_show} velas ({KLINE_INTERVAL_FOR_INDICATORS}):\n"
    for _, row in relevant_klines.iterrows():
        summary_str += f"  T: {row['timestamp'].strftime('%H:%M')}, O:{row['open']:.2f}, H:{row['high']:.2f}, L:{row['low']:.2f}, C:{row['close']:.2f}, V:{row['volume']:.2f}\n"
    return summary_str

def place_order_on_binance(si_data, qty_base, price_qt, side_val, order_type=Client.ORDER_TYPE_LIMIT):
    sym = si_data['symbol']
    try:
        fmt_qty = format_quantity(si_data, qty_base)
        min_q = _get_filter_value(si_data, 'LOT_SIZE', 'minQty')
        if min_q and fmt_qty < min_q: logging.error(f"Qty {fmt_qty} < minQty ({min_q})."); return None
        if fmt_qty <= 0: logging.error(f"Qty <=0 ({fmt_qty})."); return None
        
        params = {'symbol': sym, 'side': side_val.upper(), 'type': order_type, 'quantity': f"{fmt_qty:.8f}"} # Asegurar formato decimal
        
        # Convertir price_qt a Decimal para cálculos y comprobaciones
        price_qt_decimal = decimal.Decimal(str(price_qt))

        if order_type == Client.ORDER_TYPE_LIMIT:
            fmt_price = format_price(si_data, price_qt_decimal)
            if not check_min_notional(si_data, fmt_qty, fmt_price): return None
            params.update({'timeInForce': Client.TIME_IN_FORCE_GTC, 'price': f"{fmt_price:.8f}"}) # Asegurar formato decimal
        elif order_type == Client.ORDER_TYPE_MARKET:
            if not check_min_notional(si_data, fmt_qty, price_qt_decimal): return None # price_qt es el precio actual para notional
        else:
            logging.error(f"Tipo orden no soportado: {order_type}"); return None
        
        logging.info(f"Intentando orden: {params}"); resp = binance_client.create_order(**params)
        logging.info(f"Respuesta orden: {resp}"); return resp
    except (BinanceAPIException,BinanceOrderException) as e: logging.error(f"Error Binance orden {side_val} {sym}: {e.status_code}-{e.message}")
    except Exception as e: logging.error(f"Error inesperado orden: {e}")
    return None

# --- Funciones de IA de Google Gemini ---
def get_ai_trading_signal(market_data_summary_for_ai, current_price_val, rsi_val=None, sma20_val=None, sma50_val=None, klines_summary_str=None):
    FORZAR_SEÑAL_PARA_PRUEBA = None # ASEGÚRATE QUE ESTO SEA None PARA OPERACIÓN NORMAL CON WEB
    if FORZAR_SEÑAL_PARA_PRUEBA:
        logging.warning(f"SEÑAL IA FORZADA (INTERNA DEL BOT): {FORZAR_SEÑAL_PARA_PRUEBA}")
        return FORZAR_SEÑAL_PARA_PRUEBA
    if not model_ai:
        logging.warning("Modelo IA no disponible. Usando simulación.")
        sim_resp = ["BUY", "HOLD", "HOLD", "SELL", "HOLD"]
        s_idx = getattr(get_ai_trading_signal, "c", 0) % len(sim_resp)
        get_ai_trading_signal.c = s_idx + 1; sig_txt = sim_resp[s_idx]
        logging.info(f"Respuesta SIMULADA IA: {sig_txt}"); return sig_txt
    try:
        current_price_val_float = float(current_price_val) # IA espera float para el prompt
        prompt_parts = [
            f"Eres analista experto en trading de {SYMBOL_EXCHANGE} en Binance.",
            "Objetivo: swing trading corto plazo (horas-días), buena relación riesgo/beneficio.",
            "Prioriza capital; si no hay señal clara, HOLD.\n", "Estado Actual General:",
            market_data_summary_for_ai, "\nDatos de Mercado Adicionales:",
            f"- Precio Actual {BASE_ASSET}: {current_price_val_float:.2f} {QUOTE_ASSET}"
        ]
        if klines_summary_str: prompt_parts.append(f"- Velas Recientes: {klines_summary_str}")
        if rsi_val is not None: prompt_parts.append(f"- RSI(14): {float(rsi_val):.2f}")
        if sma20_val is not None: prompt_parts.append(f"- SMA20: {float(sma20_val):.2f}")
        if sma50_val is not None: prompt_parts.append(f"- SMA50: {float(sma50_val):.2f}")
        if sma20_val and sma50_val and current_price_val_float:
            rel_sma20 = "encima" if current_price_val_float > float(sma20_val) else ("debajo" if current_price_val_float < float(sma20_val) else "en")
            rel_sma50 = "encima" if current_price_val_float > float(sma50_val) else ("debajo" if current_price_val_float < float(sma50_val) else "en")
            prompt_parts.append(f"- Precio está {rel_sma20} SMA20 y {rel_sma50} SMA50.")
        prompt_parts.extend(["\nConsiderando TODO:", "1. Oportunidad clara de COMPRA (BUY)?",
                             "2. Oportunidad clara de VENTA (SELL)?", "3. Si no, o riesgo alto, HOLD.",
                             "Responde ÚNICAMENTE con: BUY, SELL, o HOLD."])
        prompt = "\n".join(prompt_parts)
        logging.info(f"Enviando prompt a IA ({AI_MODEL_NAME})...");
        response = model_ai.generate_content(prompt)
        sig_txt = "HOLD" 
        if response and hasattr(response, 'text') and response.text: sig_txt = response.text.strip().upper()
        elif response and hasattr(response, 'parts') and response.parts:
            full_text = "".join(part.text for part in response.parts if hasattr(part, 'text'))
            sig_txt = full_text.strip().upper()
        logging.info(f"Respuesta IA: '{sig_txt}'")
        if sig_txt in ["BUY","SELL","HOLD"]: return sig_txt
        if "BUY" in sig_txt: return "BUY"
        if "SELL" in sig_txt: return "SELL"
        logging.warning(f"Respuesta IA no reconocida: '{sig_txt}'. Defaulting to HOLD.")
        return "HOLD"
    except Exception as e: logging.error(f"Error señal IA: {e}"); return "HOLD"

def check_for_web_command():
    global current_forced_action
    try:
        if os.path.exists(COMMAND_FILE):
            with open(COMMAND_FILE, 'r') as f:
                command = f.read().strip().upper()
            if command in ["FORCE_BUY", "FORCE_SELL", "FORCE_IA_CONSULT", "CLEAR_FORCED_ACTION"]:
                current_forced_action = command
                logging.info(f"Comando web recibido: {current_forced_action}")
            else:
                logging.warning(f"Comando web desconocido: {command}")
            os.remove(COMMAND_FILE)
    except Exception as e:
        logging.error(f"Error leyendo el archivo de comando web: {e}")
        current_forced_action = None

# --- Lógica Principal del Bot ---
def run_ai_trading_bot():
    global has_position, last_buy_price, entry_timestamp, open_order_details, current_forced_action
    initialize_db_table()
    logging.info(f"Iniciando AI Trading Bot para {SYMBOL_EXCHANGE}...")
    si_data = get_binance_symbol_info(SYMBOL_EXCHANGE)
    if not si_data: logging.error(f"No info para {SYMBOL_EXCHANGE}. Saliendo."); return

    cur_price_float = None # Para P&L flotante si no hay klines en el ciclo

    while True:
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db_data_this_cycle = {col: None for col in DB_COLUMN_ORDER}
        db_data_this_cycle.update({"timestamp": datetime.now(), "simbolo": SYMBOL_EXCHANGE})
        order_action_this_cycle = False
        
        check_for_web_command()

        try:
            bal_base_ant = get_binance_asset_balance(BASE_ASSET)
            bal_qt_ant = get_binance_asset_balance(QUOTE_ASSET)
            logging.info(f"--- Nuevo Ciclo ({ts_str}) | Pos: {'Sí' if has_position else 'No'}, Ult.Compra: {last_buy_price if has_position else 'N/A'} | "
                         f"Bal {BASE_ASSET}: {bal_base_ant:.8f}, Bal {QUOTE_ASSET}: {bal_qt_ant:.4f} | "
                         f"Orden Abierta: {open_order_details['orderId'] if open_order_details else 'No'} | "
                         f"Acción Forzada Web: {current_forced_action or 'Ninguna'} ---")

            forced_action_executed_this_cycle = False
            # --- LÓGICA DE ACCIÓN FORZADA DESDE WEB ---
            if current_forced_action == "FORCE_BUY" and not has_position and not open_order_details:
                logging.warning("--- ACCIÓN FORZADA DESDE WEB: COMPRA ---")
                db_data_this_cycle["accion_bot"] = "FORZAR_COMPRA_WEB"; db_data_this_cycle["tipo_orden_ia"] = "FORZADO_WEB"
                latest_klines_df_temp = obtener_klines_df(SYMBOL_EXCHANGE, KLINE_INTERVAL_FOR_INDICATORS, 1)
                if latest_klines_df_temp is not None and not latest_klines_df_temp.empty:
                    forced_buy_price = decimal.Decimal(str(latest_klines_df_temp.iloc[-1]['close']))
                    order_res = place_order_on_binance(si_data, ORDER_AMOUNT_BASE, forced_buy_price, "BUY", Client.ORDER_TYPE_MARKET)
                    if order_res and order_res.get('status') == Client.ORDER_STATUS_FILLED:
                        has_position = True; entry_timestamp = datetime.now()
                        exec_qty = decimal.Decimal(order_res.get('executedQty','0'))
                        cum_qt_qty = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                        last_buy_price = cum_qt_qty / exec_qty if exec_qty > 0 else forced_buy_price
                        logging.info(f"COMPRA FORZADA WEB LLENADA: {exec_qty} {BASE_ASSET} a ~{last_buy_price:.4f}. ID:{order_res.get('orderId')}")
                        db_data_this_cycle.update({"precio_ejecutado": last_buy_price, "cantidad_base_ejecutada": exec_qty, "costo_total_usdt": cum_qt_qty, "orderid_abierta": str(order_res.get('orderId'))})
                    elif order_res: db_data_this_cycle["notas_adicionales"] = f"Fallo compra forzada: {order_res.get('status')}"
                    else: db_data_this_cycle["notas_adicionales"] = "Fallo API compra forzada"
                else: db_data_this_cycle["notas_adicionales"] = "Sin precio para compra forzada"
                current_forced_action = None; forced_action_executed_this_cycle = True

            elif current_forced_action == "FORCE_SELL" and has_position and not open_order_details:
                logging.warning("--- ACCIÓN FORZADA DESDE WEB: VENTA ---")
                db_data_this_cycle["accion_bot"] = "FORZAR_VENTA_WEB"; db_data_this_cycle["tipo_orden_ia"] = "FORZADO_WEB"
                latest_klines_df_temp = obtener_klines_df(SYMBOL_EXCHANGE, KLINE_INTERVAL_FOR_INDICATORS, 1)
                if latest_klines_df_temp is not None and not latest_klines_df_temp.empty:
                    forced_sell_price = decimal.Decimal(str(latest_klines_df_temp.iloc[-1]['close']))
                    current_base_bal = get_binance_asset_balance(BASE_ASSET); sell_qty_forced = min(ORDER_AMOUNT_BASE, current_base_bal)
                    if sell_qty_forced > decimal.Decimal('0'):
                        order_res = place_order_on_binance(si_data, sell_qty_forced, forced_sell_price, "SELL", Client.ORDER_TYPE_MARKET)
                        if order_res and order_res.get('status') == Client.ORDER_STATUS_FILLED:
                            exec_qty_s = decimal.Decimal(order_res.get('executedQty','0')); cum_qt_qty_s = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                            avg_s_price = cum_qt_qty_s / exec_qty_s if exec_qty_s > 0 else forced_sell_price
                            gn_ls_op = (avg_s_price - last_buy_price) * exec_qty_s if last_buy_price > 0 else decimal.Decimal('0.0')
                            logging.info(f"VENTA FORZADA WEB LLENADA: {exec_qty_s} {BASE_ASSET} a ~{avg_s_price:.4f}. G/P: {gn_ls_op:.4f}. ID:{order_res.get('orderId')}")
                            db_data_this_cycle.update({"precio_ejecutado": avg_s_price, "cantidad_base_ejecutada": exec_qty_s, "costo_total_usdt": cum_qt_qty_s, "ganancia_perdida_operacion_usdt": gn_ls_op, "orderid_abierta": str(order_res.get('orderId'))})
                            has_position = False; last_buy_price = decimal.Decimal('0.0'); entry_timestamp = None
                        elif order_res: db_data_this_cycle["notas_adicionales"] = f"Fallo venta forzada: {order_res.get('status')}"
                        else: db_data_this_cycle["notas_adicionales"] = "Fallo API venta forzada"
                    else: db_data_this_cycle["notas_adicionales"] = f"Sin saldo {BASE_ASSET} para venta forzada"
                else: db_data_this_cycle["notas_adicionales"] = "Sin precio para venta forzada"
                current_forced_action = None; forced_action_executed_this_cycle = True
            
            elif current_forced_action == "FORCE_IA_CONSULT" and not has_position and not open_order_details:
                logging.warning("--- ACCIÓN FORZADA DESDE WEB: CONSULTA IA (para Compra Potencial) ---")
                db_data_this_cycle["accion_bot"] = "FORZAR_CONSULTA_IA_WEB"
                klines_df_ia = obtener_klines_df(SYMBOL_EXCHANGE, KLINE_INTERVAL_FOR_INDICATORS, KLINE_LIMIT_FOR_INDICATORS)
                klines_df_indicado_ia = calcular_indicadores(klines_df_ia)
                if klines_df_indicado_ia is None or klines_df_indicado_ia.empty or len(klines_df_indicado_ia) < 2:
                    db_data_this_cycle["notas_adicionales"] = "Fallo datos para consulta IA forzada"
                else:
                    latest_data_ia = klines_df_indicado_ia.iloc[-1]; cur_price_ia = decimal.Decimal(str(latest_data_ia['close']))
                    rsi_actual_ia = latest_data_ia.get('RSI_14'); sma20_actual_ia = latest_data_ia.get('SMA_20'); sma50_actual_ia = latest_data_ia.get('SMA_50')
                    logging.info(f"Forzando consulta IA con Precio: {cur_price_ia:.4f}, RSI: {float(rsi_actual_ia or 0):.2f}")
                    klines_str_summary_ia = formatear_klines_para_prompt(klines_df_indicado_ia, 5)
                    mkt_sum_ai_ia = f"Actualmente no tengo una posición abierta en {SYMBOL_EXCHANGE} (Consulta IA Forzada).\n"
                    ai_sig_final_ia = get_ai_trading_signal(mkt_sum_ai_ia, cur_price_ia, rsi_actual_ia, sma20_actual_ia, sma50_actual_ia, klines_str_summary_ia)
                    logging.info(f"Señal IA (forzada consulta): {ai_sig_final_ia}")
                    db_data_this_cycle.update({"tipo_orden_ia": ai_sig_final_ia, "respuesta_ia_completa": str(ai_sig_final_ia)})
                    if ai_sig_final_ia == "BUY":
                        logging.info(f"IA (consulta forzada) decidió COMPRAR {ORDER_AMOUNT_BASE} {BASE_ASSET}...")
                        precio_compra_limit_ia = cur_price_ia * decimal.Decimal('0.999') # Orden LÍMITE
                        precio_compra_limit_formateado_ia = format_price(si_data, precio_compra_limit_ia)
                        order_res = place_order_on_binance(si_data, ORDER_AMOUNT_BASE, precio_compra_limit_formateado_ia, "BUY", Client.ORDER_TYPE_LIMIT)
                        if order_res:
                            if order_res.get('status') == Client.ORDER_STATUS_FILLED:
                                has_position = True; entry_timestamp = datetime.now(); exec_qty = decimal.Decimal(order_res.get('executedQty','0')); cum_qt_qty = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                                last_buy_price = cum_qt_qty/exec_qty if exec_qty > 0 else cur_price_ia
                                logging.info(f"COMPRA (IA consulta forzada) LLENADA: {exec_qty} {BASE_ASSET} a ~{last_buy_price:.4f}. ID:{order_res.get('orderId')}")
                                db_data_this_cycle.update({"accion_bot":"COMPRA_EJECUTADA_IA_FORZADA_WEB", "precio_ejecutado":last_buy_price, "cantidad_base_ejecutada":exec_qty, "costo_total_usdt":cum_qt_qty, "orderid_abierta": str(order_res.get('orderId'))})
                            elif order_res.get('status') in [Client.ORDER_STATUS_NEW, Client.ORDER_STATUS_PARTIALLY_FILLED]:
                                open_order_details = {'orderId': order_res['orderId'], 'side': 'BUY', 'price': decimal.Decimal(order_res['price']), 'qty': decimal.Decimal(order_res['origQty']), 'timestamp': datetime.now()}
                                db_data_this_cycle.update({"accion_bot":"COMPRA_ORDEN_ABIERTA_IA_FORZADA_WEB", "orderid_abierta": str(order_res['orderId'])})
                            else: db_data_this_cycle.update({"notas_adicionales":f"Fallo colocar compra IA forzada: {order_res.get('status')}"})
                        else: db_data_this_cycle.update({"notas_adicionales":"Fallo API compra IA forzada"})
                    else: logging.info(f"IA (consulta forzada) decidió {ai_sig_final_ia}. No se realiza compra.")
                current_forced_action = None; forced_action_executed_this_cycle = True

            elif current_forced_action == "CLEAR_FORCED_ACTION":
                logging.info("Acción forzada web limpiada."); current_forced_action = None
            
            if forced_action_executed_this_cycle:
                # No es necesario obtener klines de nuevo si ya se hizo para la acción forzada
                pass # La lógica de guardado de estado y BD se hace al final del try
            # --- FIN LÓGICA DE ACCIÓN FORZADA ---

            # Si no se ejecutó una acción forzada que requiera 'continue', sigue la lógica normal
            if not forced_action_executed_this_cycle:
                if open_order_details: # GESTIONAR ORDEN ABIERTA (LÓGICA NORMAL)
                    order_action_this_cycle = True
                    logging.info(f"Verificando orden ID: {open_order_details['orderId']} ({open_order_details['side']})")
                    db_data_this_cycle["orderid_abierta"] = str(open_order_details['orderId'])
                    try:
                        order_status_info = binance_client.get_order(symbol=SYMBOL_EXCHANGE, orderId=open_order_details['orderId'])
                        logging.info(f"Estado orden {open_order_details['orderId']}: {order_status_info['status']}")
                        if order_status_info['status'] == Client.ORDER_STATUS_FILLED:
                            logging.info(f"¡Orden {open_order_details['orderId']} ({open_order_details['side']}) LLENADA!")
                            exec_qty = decimal.Decimal(order_status_info.get('executedQty','0'))
                            cum_qt_qty = decimal.Decimal(order_status_info.get('cummulativeQuoteQty','0'))
                            avg_filled_price = cum_qt_qty / exec_qty if exec_qty > 0 else open_order_details['price']
                            db_data_this_cycle.update({"precio_ejecutado": avg_filled_price, "cantidad_base_ejecutada": exec_qty, "costo_total_usdt": cum_qt_qty})
                            if open_order_details['side'] == 'BUY':
                                has_position = True; last_buy_price = avg_filled_price; entry_timestamp = datetime.now()
                                logging.info(f"COMPRA COMPLETADA (previa): {exec_qty} {BASE_ASSET} a ~{last_buy_price:.4f}.")
                                db_data_this_cycle["accion_bot"] = "COMPRA_LLENADA_PREVIA"
                            elif open_order_details['side'] == 'SELL':
                                gn_ls_op = (avg_filled_price - last_buy_price) * exec_qty if last_buy_price > 0 else decimal.Decimal('0.0')
                                logging.info(f"VENTA COMPLETADA (previa): {exec_qty} {BASE_ASSET} a ~{avg_filled_price:.4f}. G/P: {gn_ls_op:.4f}.")
                                db_data_this_cycle.update({"accion_bot": "VENTA_LLENADA_PREVIA", "ganancia_perdida_operacion_usdt": gn_ls_op})
                                has_position = False; last_buy_price = decimal.Decimal('0.0'); entry_timestamp = None
                            open_order_details = None
                        elif order_status_info['status'] in [Client.ORDER_STATUS_CANCELED, Client.ORDER_STATUS_EXPIRED, Client.ORDER_STATUS_REJECTED, Client.ORDER_STATUS_PENDING_CANCEL]:
                            logging.warning(f"Orden {open_order_details['orderId']} no activa o cancelada. Estado: {order_status_info['status']}")
                            db_data_this_cycle.update({"accion_bot":f"ORDEN_FALLIDA_O_CANCELADA_PREVIA ({order_status_info['status']})", "notas_adicionales": f"OrderID: {open_order_details['orderId']}"})
                            open_order_details = None
                        else: 
                            time_since_order = datetime.now() - open_order_details['timestamp']
                            if time_since_order > timedelta(minutes=ORDER_TIMEOUT_MINUTES):
                                logging.warning(f"Orden {open_order_details['orderId']} timeout. Cancelando...")
                                try: 
                                    binance_client.cancel_order(symbol=SYMBOL_EXCHANGE, orderId=open_order_details['orderId'])
                                    logging.info(f"Orden {open_order_details['orderId']} cancelada.")
                                    db_data_this_cycle.update({"accion_bot":"ORDEN_CANCELADA_TIMEOUT", "notas_adicionales": f"OrderID: {open_order_details['orderId']}"})
                                except Exception as e_cancel: 
                                    logging.error(f"Error cancelando orden {open_order_details['orderId']}: {e_cancel}")
                                    db_data_this_cycle.update({"accion_bot":"ERROR_CANCELAR_ORDEN", "notas_adicionales": f"ID: {open_order_details['orderId']}, Err: {e_cancel}"})
                                open_order_details = None
                            else:
                                logging.info(f"Orden {open_order_details['orderId']} ({order_status_info['status']}) abierta. Tiempo: {time_since_order}.")
                                db_data_this_cycle.update({"accion_bot":"ESPERANDO_ORDEN_ABIERTA", "notas_adicionales": f"OrderID: {open_order_details['orderId']}"})
                    except Exception as e_get_order:
                        logging.error(f"Error verificando orden {open_order_details['orderId']}: {e_get_order}")
                        db_data_this_cycle.update({"accion_bot":"ERROR_VERIFICAR_ORDEN", "notas_adicionales": f"ID: {open_order_details['orderId']}, Err: {e_get_order}"})

                # LÓGICA NORMAL DE TRADING (SI NO HAY ORDEN ABIERTA GESTIONADA)
                elif not open_order_details: # Asegurar que solo se ejecuta si no hay orden abierta pendiente de la lógica anterior
                    klines_df_trading = obtener_klines_df(SYMBOL_EXCHANGE, KLINE_INTERVAL_FOR_INDICATORS, KLINE_LIMIT_FOR_CHART) # Usar KLINE_LIMIT_FOR_CHART
                    klines_df_indicado = calcular_indicadores(klines_df_trading) # Calcular indicadores sobre este df

                    # --- GUARDAR DATOS PARA EL GRÁFICO ---
                    if klines_df_indicado is not None and not klines_df_indicado.empty:
                        df_for_chart = klines_df_indicado.tail(MAX_CHART_POINTS).copy()
                        df_for_chart['time'] = df_for_chart['timestamp'].apply(lambda x: int(x.timestamp())) 
                        chart_points_ohlc = df_for_chart[['time', 'open', 'high', 'low', 'close']].to_dict(orient='records')
                        try:
                            with open(CHART_DATA_FILE, 'w') as f_chart: json.dump(chart_points_ohlc, f_chart)
                            logging.debug(f"Datos del gráfico OHLC actualizados en {CHART_DATA_FILE}")
                        except Exception as e_chart: logging.error(f"Error escribiendo datos del gráfico: {e_chart}")
                    # --- FIN GUARDAR DATOS PARA EL GRÁFICO ---

                    if klines_df_indicado is None or klines_df_indicado.empty or len(klines_df_indicado) < KLINE_LIMIT_FOR_INDICATORS: # Chequear contra límite de indicadores
                        db_data_this_cycle.update({"accion_bot":"ERROR_DATOS_INDICADORES", "notas_adicionales":"Insuficientes datos."})
                    else:
                        latest_data = klines_df_indicado.iloc[-1]; prev_data = klines_df_indicado.iloc[-2]
                        cur_price_float = latest_data['close']; rsi_actual = latest_data.get('RSI_14')
                        sma20_actual = latest_data.get('SMA_20'); sma50_actual = latest_data.get('SMA_50')
                        rsi_display_str = f"{float(rsi_actual or 0):.2f}"
                        logging.info(f"Precio actual (cierre últ. vela): {cur_price_float:.4f}. RSI: {rsi_display_str}")

                        if not has_position: # LÓGICA DE ENTRADA (COMPRA NORMAL)
                            condicion_pre_filtro_compra = False
                            # ASEGÚRATE QUE ESTA ES TU CONDICIÓN DE PRE-FILTRO REAL
                            if rsi_actual is not None and sma20_actual is not None and prev_data.get('SMA_20') is not None and prev_data.get('close') is not None:
                                if float(rsi_actual) < 40 and float(prev_data['close']) < float(prev_data['SMA_20']) and cur_price_float > float(sma20_actual):
                                     condicion_pre_filtro_compra = True
                                     logging.info(f"PRE-FILTRO COMPRA ACTIVADO: RSI={float(rsi_actual):.2f}, Precio cruzó SMA20.")
                            
                            if condicion_pre_filtro_compra:
                                logging.info("Consultando IA para confirmación de COMPRA...")
                                klines_str_summary = formatear_klines_para_prompt(klines_df_indicado, 5)
                                mkt_sum_ai = f"Actualmente no tengo una posición abierta en {SYMBOL_EXCHANGE}.\n"
                                # ASEGÚRATE QUE FORZAR_SEÑAL_PARA_PRUEBA en get_ai_trading_signal es None
                                ai_sig_final = get_ai_trading_signal(mkt_sum_ai, decimal.Decimal(str(cur_price_float)), rsi_actual, sma20_actual, sma50_actual, klines_str_summary)
                                logging.info(f"Señal IA para entrada: {ai_sig_final}")
                                db_data_this_cycle.update({"tipo_orden_ia": ai_sig_final, "respuesta_ia_completa": str(ai_sig_final)})
                                
                                if ai_sig_final == "BUY":
                                    db_data_this_cycle["accion_bot"] = "INTENTO_COMPRA_IA"; logging.info(f"IA: COMPRAR {ORDER_AMOUNT_BASE} {BASE_ASSET}...")
                                    precio_compra_limit = decimal.Decimal(str(cur_price_float)) * decimal.Decimal('0.999')
                                    precio_compra_limit_formateado = format_price(si_data, precio_compra_limit)
                                    order_res = place_order_on_binance(si_data, ORDER_AMOUNT_BASE, precio_compra_limit_formateado, "BUY", Client.ORDER_TYPE_LIMIT)
                                    if order_res:
                                        if order_res.get('status') == Client.ORDER_STATUS_FILLED:
                                            has_position = True; entry_timestamp = datetime.now(); exec_qty = decimal.Decimal(order_res.get('executedQty','0')); cum_qt_qty = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                                            last_buy_price = cum_qt_qty/exec_qty if exec_qty > 0 else decimal.Decimal(order_res.get('price',str(cur_price_float)))
                                            logging.info(f"COMPRA LLENADA INMEDIATAMENTE (IA): {exec_qty} {BASE_ASSET} a ~{last_buy_price:.4f}. ID:{order_res.get('orderId')}")
                                            db_data_this_cycle.update({"accion_bot":"COMPRA_EJECUTADA_IA", "precio_ejecutado":last_buy_price, "cantidad_base_ejecutada":exec_qty, "costo_total_usdt":cum_qt_qty, "orderid_abierta": str(order_res.get('orderId'))})
                                        elif order_res.get('status') in [Client.ORDER_STATUS_NEW, Client.ORDER_STATUS_PARTIALLY_FILLED]:
                                            open_order_details = {'orderId': order_res['orderId'], 'side': 'BUY', 'price': decimal.Decimal(order_res['price']), 'qty': decimal.Decimal(order_res['origQty']), 'timestamp': datetime.now()}
                                            db_data_this_cycle.update({"accion_bot":"COMPRA_ORDEN_ABIERTA_IA", "orderid_abierta": str(order_res['orderId'])})
                                        else: db_data_this_cycle.update({"accion_bot":"FALLO_COLOCAR_COMPRA_IA", "notas_adicionales":f"Resp: {order_res.get('status')}"})
                                    else: db_data_this_cycle.update({"accion_bot":"FALLO_COMPRA_API_IA", "notas_adicionales":"place_order_on_binance devolvió None"})
                                else: # IA no confirmó BUY
                                    db_data_this_cycle["accion_bot"] = f"IA_NO_CONFIRMA_COMPRA ({ai_sig_final})"
                                    logging.info(f"IA no confirma compra ({ai_sig_final}). Pre-filtro fue TRUE.")
                            else: # Pre-filtro no se activó
                                db_data_this_cycle["accion_bot"] = "PREFILTRO_NO_COMPRA"
                                db_data_this_cycle["tipo_orden_ia"] = "N/A_PREFILTRO"

                        elif has_position: # LÓGICA DE SALIDA (TP/SL NORMAL)
                            db_data_this_cycle.update({"tipo_orden_ia": "N/A_REGLAS_SALIDA", "respuesta_ia_completa": "Salida por reglas TP/SL"})
                            target_sell_price_tp = last_buy_price * (decimal.Decimal('1') + TARGET_PROFIT_PERCENT / decimal.Decimal('100'))
                            target_sell_price_sl = last_buy_price * (decimal.Decimal('1') - STOP_LOSS_PERCENT / decimal.Decimal('100'))
                            current_real_price_decimal = decimal.Decimal(str(cur_price_float))
                            price_to_check_conditions = current_real_price_decimal # ASEGÚRATE QUE NO HAY FORZADO DE PRECIO AQUÍ
                            logging.info(f"Posición abierta. Compra: {last_buy_price:.4f}. Actual: {price_to_check_conditions:.4f}. TP: {target_sell_price_tp:.4f}, SL: {target_sell_price_sl:.4f}")
                            accion_salida = None; precio_salida_orden = None; tipo_salida = ""; orden_tipo_salida = None
                            if price_to_check_conditions >= target_sell_price_tp:
                                accion_salida = True; precio_salida_orden = target_sell_price_tp 
                                tipo_salida = "TAKE_PROFIT"; orden_tipo_salida = Client.ORDER_TYPE_LIMIT
                                logging.info(f"¡TAKE PROFIT ALCANZADO! Intentando vender con orden LIMIT a {precio_salida_orden:.4f}.")
                            elif price_to_check_conditions <= target_sell_price_sl:
                                accion_salida = True; precio_salida_orden = price_to_check_conditions # Usar precio actual para orden MARKET
                                tipo_salida = "STOP_LOSS"; orden_tipo_salida = Client.ORDER_TYPE_MARKET
                                logging.warning(f"¡STOP LOSS ALCANZADO! Intentando vender con orden MARKET.")
                            if accion_salida:
                                db_data_this_cycle["accion_bot"] = f"INTENTO_VENTA_{tipo_salida}"
                                precio_orden_formateado = format_price(si_data, precio_salida_orden)
                                current_base_balance = get_binance_asset_balance(BASE_ASSET); sell_qty = min(ORDER_AMOUNT_BASE, current_base_balance)
                                if sell_qty <= decimal.Decimal('0'):
                                    logging.warning(f"No hay {BASE_ASSET} para vender. Saldo: {current_base_balance}")
                                    db_data_this_cycle.update({"accion_bot":f"FALLO_VENTA_{tipo_salida}_NO_SALDO"}); has_position = False; last_buy_price = decimal.Decimal('0.0'); entry_timestamp = None
                                else:
                                    order_res = place_order_on_binance(si_data, sell_qty, precio_orden_formateado, "SELL", order_type=orden_tipo_salida)
                                    if order_res:
                                        if order_res.get('status') == Client.ORDER_STATUS_FILLED:
                                            exec_qty_s = decimal.Decimal(order_res.get('executedQty','0')); cum_qt_qty_s = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                                            avg_s_price = cum_qt_qty_s/exec_qty_s if exec_qty_s > 0 else decimal.Decimal(order_res.get('price',str(cur_price_float)))
                                            gn_ls_op = (avg_s_price-last_buy_price)*exec_qty_s if last_buy_price > 0 else decimal.Decimal('0.0')
                                            logging.info(f"VENTA {tipo_salida} LLENADA: {exec_qty_s} {BASE_ASSET} a ~{avg_s_price:.4f}. G/P: {gn_ls_op:.4f}. ID:{order_res.get('orderId')}")
                                            db_data_this_cycle.update({"accion_bot":f"VENTA_EJECUTADA_{tipo_salida}", "precio_ejecutado":avg_s_price, "cantidad_base_ejecutada":exec_qty_s, "costo_total_usdt":cum_qt_qty_s, "ganancia_perdida_operacion_usdt":gn_ls_op, "orderid_abierta": str(order_res.get('orderId'))})
                                            has_position=False; last_buy_price=decimal.Decimal('0.0'); entry_timestamp = None
                                        elif order_res.get('status') in [Client.ORDER_STATUS_NEW, Client.ORDER_STATUS_PARTIALLY_FILLED] and orden_tipo_salida == Client.ORDER_TYPE_LIMIT:
                                            open_order_details = {'orderId': order_res['orderId'], 'side': 'SELL', 'price': decimal.Decimal(order_res['price']), 'qty': decimal.Decimal(order_res['origQty']), 'timestamp': datetime.now()}
                                            db_data_this_cycle.update({"accion_bot":f"VENTA_ORDEN_ABIERTA_{tipo_salida}", "orderid_abierta": str(order_res['orderId'])})
                                        else: db_data_this_cycle.update({"accion_bot":f"FALLO_COLOCAR_VENTA_{tipo_salida}", "notas_adicionales":f"Resp: {order_res.get('status')}"})
                                    else: db_data_this_cycle.update({"accion_bot":f"FALLO_VENTA_API_{tipo_salida}", "notas_adicionales":"place_order_on_binance devolvió None"})
                            else: # Ni TP ni SL
                                if db_data_this_cycle.get("accion_bot") is None: db_data_this_cycle["accion_bot"] = "MANTENER_POSICION_ABIERTA"
                                logging.info("Manteniendo posición. Ni TP ni SL alcanzados.")
            
            # --- Actualizar datos finales para BD y ESTADO DEL BOT ---
            if db_data_this_cycle.get("accion_bot") is None and not open_order_details and not order_action_this_cycle and not forced_action_executed_this_cycle :
                db_data_this_cycle["accion_bot"] = "CICLO_SIN_ACCION_NOTABLE" # Si no se hizo nada más

            # Balances para el log y para el estado del bot
            bal_base_desp_log = get_binance_asset_balance(BASE_ASSET)
            bal_qt_desp_log = get_binance_asset_balance(QUOTE_ASSET)
            db_data_this_cycle.update({
                "tiene_posicion_despues": has_position, 
                "precio_ultima_compra_despues": last_buy_price if has_position and last_buy_price > 0 else None,
                "balance_base_despues": bal_base_desp_log, 
                "balance_quote_despues": bal_qt_desp_log,
                "orderid_abierta": str(open_order_details['orderId']) if open_order_details else db_data_this_cycle.get("orderid_abierta") # Mantener si ya se llenó una orden en este ciclo
            })
            for col in DB_COLUMN_ORDER: # Asegurar que todas las columnas para la BD tienen un valor
                if col not in db_data_this_cycle: db_data_this_cycle[col] = None
            log_to_db(db_data_this_cycle)

            # --- GUARDAR ESTADO DEL BOT (se repite aquí para asegurar que se escribe después de todas las acciones del ciclo) ---
            current_pnl_usdt_status = decimal.Decimal('0.0')
            current_pnl_percent_status = decimal.Decimal('0.0')
            time_in_position_str_status = "N/A"
            tp_price_status = None
            sl_price_status = None

            if has_position and last_buy_price > 0:
                cur_price_for_pnl_float_status = None
                # Intenta usar el cur_price_float ya obtenido si está disponible en el scope del ciclo
                if 'cur_price_float' in locals() and cur_price_float is not None:
                    cur_price_for_pnl_float_status = cur_price_float
                elif klines_df_indicado is not None and not klines_df_indicado.empty : # 'klines_df_indicado' debería estar disponible si se procesó la lógica normal
                     cur_price_for_pnl_float_status = klines_df_indicado.iloc[-1]['close']
                else: # Fallback si no hay klines procesados en este ciclo (ej. solo se gestionó orden abierta)
                    temp_klines_pnl = obtener_klines_df(SYMBOL_EXCHANGE, Client.KLINE_INTERVAL_1MINUTE, 1)
                    if temp_klines_pnl is not None and not temp_klines_pnl.empty:
                        cur_price_for_pnl_float_status = temp_klines_pnl.iloc[-1]['close']
                
                if cur_price_for_pnl_float_status is not None:
                    current_pnl_usdt_status = (decimal.Decimal(str(cur_price_for_pnl_float_status)) - last_buy_price) * ORDER_AMOUNT_BASE
                    costo_compra_total_status = last_buy_price * ORDER_AMOUNT_BASE
                    if costo_compra_total_status > 0 :
                        current_pnl_percent_status = (current_pnl_usdt_status / costo_compra_total_status) * 100
                
                if entry_timestamp:
                   time_in_pos = datetime.now() - entry_timestamp
                   time_in_position_str_status = str(time_in_pos).split('.')[0]
                
                tp_price_status = float(last_buy_price * (1 + TARGET_PROFIT_PERCENT/100))
                sl_price_status = float(last_buy_price * (1 - STOP_LOSS_PERCENT/100))

            bot_status_data = {
                "has_position": has_position,
                "last_buy_price": float(last_buy_price) if last_buy_price > 0 else None,
                "pnl_usdt": float(current_pnl_usdt_status),
                "pnl_percent": float(current_pnl_percent_status),
                "target_profit_price": tp_price_status,
                "stop_loss_price": sl_price_status,
                "time_in_position": time_in_position_str_status,
                "open_order_id": open_order_details['orderId'] if open_order_details else None,
                "base_asset_balance": float(bal_base_desp_log), # Usar los balances ya obtenidos para consistencia
                "quote_asset_balance": float(bal_qt_desp_log),
                "last_bot_action": db_data_this_cycle.get("accion_bot", "N/A"),
                "timestamp": datetime.now().isoformat(),
                "check_interval_seconds": CHECK_INTERVAL # <-- AÑADIR ESTO

            }
            try:
                with open(BOT_STATUS_FILE, 'w') as f_s: json.dump(bot_status_data, f_s)
                logging.debug(f"Estado del bot actualizado en {BOT_STATUS_FILE}")
            except Exception as e_s: logging.error(f"Error escribiendo estado del bot: {e_s}")
            # --- FIN GUARDAR ESTADO DEL BOT ---

        except KeyboardInterrupt: logging.info("Bot detenido por el usuario."); break
        except BinanceAPIException as bae:
            logging.error(f"Error API Binance: {bae.status_code} - {bae.message}", exc_info=True)
            db_err_data = {c:None for c in DB_COLUMN_ORDER}; db_err_data.update({"timestamp":datetime.now(),"accion_bot":"ERROR_BINANCE_API", "simbolo":SYMBOL_EXCHANGE,"notas_adicionales":f"{bae.status_code}-{bae.message}"[:200]})
            log_to_db(db_err_data); time.sleep(CHECK_INTERVAL)
        except Exception as e:
            logging.error(f"Error catastrófico en ciclo principal: {e}", exc_info=True)
            db_err_data = {c:None for c in DB_COLUMN_ORDER}; db_err_data.update({"timestamp":datetime.now(),"accion_bot":"ERROR_CATASTROFICO_CICLO", "simbolo":SYMBOL_EXCHANGE,"notas_adicionales":str(e)[:500]})
            log_to_db(db_err_data); time.sleep(max(CHECK_INTERVAL, 120)) # Pausa más larga
        
        logging.info(f"--- Fin de Ciclo --- Esperando {CHECK_INTERVAL} segundos...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    if not DATABASE_URL: logging.warning("DATABASE_URL no configurada.")
    if not GOOGLE_AI_API_KEY: logging.warning("GOOGLE_AI_API_KEY no configurada. IA usará simulación.")
    api_keys_ok = True
    if not BINANCE_API_KEY or not BINANCE_API_SECRET: 
        logging.error("Claves API Binance no configuradas. Saliendo."); api_keys_ok = False
    if api_keys_ok:
        try: run_ai_trading_bot()
        except Exception as main_e: logging.critical(f"Excepción no controlada a nivel de main: {main_e}", exc_info=True)
        finally: logging.info("Bot finalizado.")
    else: logging.error("Bot no se ejecutará por falta de claves API.")