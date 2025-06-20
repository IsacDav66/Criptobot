# --- START OF FILE gemini_bot.py ---

import os
import time
import logging
import decimal
from datetime import datetime, timedelta

# Para la IA de Google Gemini
import google.generativeai as genai

# Para Binance Exchange
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException

# Para cargar .env
from dotenv import load_dotenv
load_dotenv()

# Para PostgreSQL
import psycopg2
import psycopg2.extras # Para dict cursor

# --- Configuración General ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuración de Base de Datos PostgreSQL ---
# La URL de conexión se obtiene de una variable de entorno para seguridad
DATABASE_URL = os.environ.get("DATABASE_URL_BOT")
# Si no está en variable de entorno, puedes ponerla aquí directamente, pero no es lo ideal para producción
# DATABASE_URL = "postgresql://isacdav:fajfEGwevDjOvqIQqPzvS4ds7jI6BDOj@dpg-d0sd3cidbo4c73evf780-a.ohio-postgres.render.com/basebot"

DB_TABLE_NAME = 'trading_log' # Nombre de la tabla donde se guardarán los logs

# Columnas para la tabla y el orden de inserción (similar a los headers del Excel)
# Adaptaremos los tipos de datos para PostgreSQL
DB_COLUMNS_TYPES = {
    "timestamp": "TIMESTAMP WITH TIME ZONE",
    "accion_bot": "VARCHAR(50)",
    "simbolo": "VARCHAR(15)",
    "precio_ejecutado": "DECIMAL(20, 8)", # DECIMAL(precision, escala)
    "cantidad_base_ejecutada": "DECIMAL(20, 8)",
    "costo_total_usdt": "DECIMAL(20, 8)",
    "tipo_orden_ia": "VARCHAR(10)",
    "respuesta_ia_completa": "TEXT", # Para prompts o respuestas largas
    "tiene_posicion_despues": "BOOLEAN",
    "precio_ultima_compra_despues": "DECIMAL(20, 8)",
    "balance_base_despues": "DECIMAL(20, 8)", # Asumiendo BTC como base
    "balance_quote_despues": "DECIMAL(20, 8)", # Asumiendo USDT como quote
    "ganancia_perdida_operacion_usdt": "DECIMAL(20, 8)",
    "orderid_abierta": "VARCHAR(50)", # Binance order IDs pueden ser largos
    "notas_adicionales": "TEXT"
}
DB_COLUMN_ORDER = list(DB_COLUMNS_TYPES.keys()) # Mantiene el orden para la inserción


# ... (Configuración de IA de Google Gemini como estaba) ...
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")
if GOOGLE_AI_API_KEY:
    genai.configure(api_key=GOOGLE_AI_API_KEY)
    AI_MODEL_NAME = 'gemini-1.5-flash-latest'
    try:
        model_ai = genai.GenerativeModel(AI_MODEL_NAME)
        logging.info(f"Modelo de IA Gemini '{AI_MODEL_NAME}' cargado correctamente.")
    except Exception as e:
        logging.error(f"Error al cargar el modelo de IA Gemini '{AI_MODEL_NAME}': {e}")
        model_ai = None
else:
    logging.warning("GOOGLE_AI_API_KEY no encontrada. IA usará simulación.")
    model_ai = None

# ... (Configuración de Binance como estaba) ...
USE_TESTNET = True
if USE_TESTNET:
    BINANCE_API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY")
    BINANCE_API_SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET")
    logging.info("--- USANDO BINANCE TESTNET ---")
else:
    BINANCE_API_KEY = os.environ.get("BINANCE_PROD_API_KEY")
    BINANCE_API_SECRET = os.environ.get("BINANCE_PROD_API_SECRET")
    logging.info("--- USANDO BINANCE PRODUCCIÓN (DINERO REAL) ---")

if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    logging.error("Claves API Binance no encontradas. Saliendo.")
    exit()

try:
    binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=USE_TESTNET)
    binance_client.ping()
    logging.info(f"Conexión a Binance {'Testnet' if USE_TESTNET else 'Producción'} exitosa.")
    server_time_offset = int(binance_client.get_server_time()['serverTime']) - int(time.time() * 1000)
    logging.info(f"Desfase horario con servidor Binance: {server_time_offset} ms")
except Exception as e:
    logging.error(f"Error conectando a Binance: {e}")
    exit()

# ... (Parámetros de Trading como estaban) ...
SYMBOL_EXCHANGE = 'BTCUSDT'
BASE_ASSET = 'BTC'
QUOTE_ASSET = 'USDT'
ORDER_AMOUNT_BASE = decimal.Decimal('0.0002')
CHECK_INTERVAL = 60
MIN_PROFIT_PERCENT_FOR_AI_SELL = decimal.Decimal('0.8')
ORDER_TIMEOUT_MINUTES = 15

# ... (Estado del Bot como estaba) ...
has_position = False
last_buy_price = decimal.Decimal('0.0')
symbol_info_cache = {}
open_order_details = None


# --- Funciones de Base de Datos PostgreSQL ---
def get_db_connection():
    """Establece y devuelve una conexión a la base de datos."""
    if not DATABASE_URL:
        logging.error("DATABASE_URL no está configurada. No se puede conectar a la BD.")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logging.error(f"Error conectando a la base de datos PostgreSQL: {e}")
        return None

def initialize_db_table():
    """Crea la tabla de log en la BD si no existe."""
    conn = get_db_connection()
    if not conn:
        return

    # Construir el comando CREATE TABLE dinámicamente
    columns_sql = ",\n".join([f'"{col_name}" {col_type}' for col_name, col_type in DB_COLUMNS_TYPES.items()])
    # Añadir una columna id autoincremental como clave primaria
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {DB_TABLE_NAME} (
        id SERIAL PRIMARY KEY,
        {columns_sql}
    );
    """
    try:
        with conn.cursor() as cur:
            cur.execute(create_table_sql)
        conn.commit()
        logging.info(f"Tabla '{DB_TABLE_NAME}' verificada/creada en la base de datos.")
    except Exception as e:
        logging.error(f"Error creando/verificando tabla '{DB_TABLE_NAME}': {e}")
    finally:
        if conn:
            conn.close()

def log_to_db(data_dict):
    """Inserta una fila de datos en la tabla de la BD."""
    conn = get_db_connection()
    if not conn:
        return

    # Asegurarse de que todos los valores sean del tipo correcto o None
    # y que el diccionario tenga todas las claves esperadas en el orden correcto
    values_to_insert = []
    for col_name in DB_COLUMN_ORDER:
        value = data_dict.get(col_name)
        # Convertir Decimal a string para psycopg2 o manejar None
        if isinstance(value, decimal.Decimal):
            values_to_insert.append(value) # psycopg2 puede manejar Decimals directamente
        elif value is None or value == "N/A" or str(value).strip() == "":
            # Para columnas numéricas/booleanas, si el valor es "N/A" o vacío, insertar NULL
            if DB_COLUMNS_TYPES.get(col_name, "").startswith("DECIMAL") or DB_COLUMNS_TYPES.get(col_name) == "BOOLEAN":
                values_to_insert.append(None)
            else:
                values_to_insert.append(str(value) if value is not None else None) # Strings o Nones
        elif isinstance(value, bool):
             values_to_insert.append(value)
        else:
            values_to_insert.append(str(value)) # Convertir otros tipos a string si es necesario

    # Construir el comando INSERT SQL
    columns_str = ", ".join([f'"{col}"' for col in DB_COLUMN_ORDER])
    placeholders_str = ", ".join(["%s"] * len(DB_COLUMN_ORDER))
    insert_sql = f"INSERT INTO {DB_TABLE_NAME} ({columns_str}) VALUES ({placeholders_str});"

    try:
        with conn.cursor() as cur:
            cur.execute(insert_sql, tuple(values_to_insert))
        conn.commit()
        logging.debug(f"Datos insertados en tabla '{DB_TABLE_NAME}'.")
    except Exception as e:
        logging.error(f"Error insertando datos en '{DB_TABLE_NAME}': {e}")
        logging.error(f"Datos que se intentaron insertar: {dict(zip(DB_COLUMN_ORDER, values_to_insert))}")
    finally:
        if conn:
            conn.close()

# ... (Funciones Auxiliares de Binance, Interacción con Binance, IA como estaban) ...
# (get_binance_symbol_info, _get_filter_value, format_quantity, format_price, check_min_notional)
# (get_binance_asset_balance, get_current_price_from_binance)
# (obtener_klines_recientes, calcular_rsi_desde_klines, etc. - placeholders)
# (place_order_on_binance, get_ai_trading_signal)
# (Asegúrate que place_order_on_binance usa ORDER_TYPE_LIMIT por defecto y get_ai_trading_signal no fuerza señales)
def get_binance_symbol_info(symbol):
    if symbol not in symbol_info_cache:
        try: info = binance_client.get_symbol_info(symbol); symbol_info_cache[symbol] = info
        except BinanceAPIException as e: logging.error(f"Error API info {symbol}: {e}"); return None
        logging.info(f"Info y filtros para {symbol} cacheados.")
    return symbol_info_cache[symbol]

def _get_filter_value(symbol_info, filter_type, filter_key):
    if not symbol_info or 'filters' not in symbol_info: return None
    for f in symbol_info['filters']:
        if f['filterType'] == filter_type: return decimal.Decimal(str(f.get(filter_key, '0')))
    return None

def format_quantity(si_data, qty):
    ss = _get_filter_value(si_data, 'LOT_SIZE', 'stepSize')
    return (decimal.Decimal(str(qty))//ss)*ss if ss and ss!=0 else decimal.Decimal(str(qty))

def format_price(si_data, price):
    ts = _get_filter_value(si_data, 'PRICE_FILTER', 'tickSize')
    return (decimal.Decimal(str(price))//ts)*ts if ts and ts!=0 else decimal.Decimal(str(price))

def check_min_notional(si_data, qty, price):
    mn_val = _get_filter_value(si_data, 'NOTIONAL', 'minNotional')
    if mn_val is None: logging.warning(f"No se pudo obtener minNotional para {si_data.get('symbol','N/A')}. Asume OK."); return True
    cur_not = decimal.Decimal(str(qty)) * decimal.Decimal(str(price))
    if cur_not < mn_val: logging.warning(f"Orden no cumple MIN_NOTIONAL ({mn_val}). Nocional: {cur_not}"); return False
    logging.debug(f"MIN_NOTIONAL: {cur_not} >= {mn_val}. OK.")
    return True

def get_binance_asset_balance(asset_symbol):
    try:
        bal_info = binance_client.get_asset_balance(asset=asset_symbol)
        return decimal.Decimal(bal_info['free']) if bal_info and 'free' in bal_info else decimal.Decimal('0.0')
    except Exception as e: logging.error(f"Error obteniendo balance {asset_symbol}: {e}"); return decimal.Decimal('0.0')

def get_current_price_from_binance(symbol):
    try: ticker = binance_client.get_symbol_ticker(symbol=symbol); return {"current_price": decimal.Decimal(ticker['price'])}
    except Exception as e: logging.error(f"Error obteniendo precio {symbol}: {e}"); return None

def obtener_klines_recientes(symbol, interval, limit):
    try: return None # Placeholder
    except Exception as e: logging.error(f"Error obteniendo klines para {symbol}: {e}"); return None

def calcular_rsi_desde_klines(klines, period=14):
    if not klines or len(klines) < period: return None
    return None # Placeholder

def calcular_sma_desde_klines(klines, period=20):
    if not klines or len(klines) < period: return None
    return None # Placeholder

def formatear_klines_para_prompt(klines, num_klines_to_show=3):
    if not klines: return "No hay datos de klines."
    return "Datos de velas no procesados." # Placeholder

def place_order_on_binance(si_data, qty_base, price_qt, side_val):
    sym = si_data['symbol']
    try:
        fmt_qty = format_quantity(si_data, qty_base)
        min_q = _get_filter_value(si_data, 'LOT_SIZE', 'minQty')
        if min_q and fmt_qty < min_q: logging.error(f"Qty {fmt_qty} < minQty ({min_q})."); return None
        if fmt_qty <= 0: logging.error(f"Qty <=0 ({fmt_qty})."); return None
        ORDER_TYPE_TO_USE = Client.ORDER_TYPE_LIMIT
        if ORDER_TYPE_TO_USE == Client.ORDER_TYPE_LIMIT:
            fmt_price = format_price(si_data, price_qt)
            if not check_min_notional(si_data, fmt_qty, fmt_price): return None
            params = {'symbol': sym, 'side': side_val.upper(), 'type': ORDER_TYPE_TO_USE,
                      'timeInForce': Client.TIME_IN_FORCE_GTC,
                      'quantity': f"{fmt_qty}", 'price': f"{fmt_price}"}
        else: logging.error(f"Tipo de orden no soportado: {ORDER_TYPE_TO_USE}"); return None
        logging.info(f"Intentando orden: {params}")
        resp = binance_client.create_order(**params)
        logging.info(f"Respuesta orden: {resp}")
        return resp
    except (BinanceAPIException, BinanceOrderException) as e: logging.error(f"Error Binance orden {side_val} {sym}: {e.status_code}-{e.message}")
    except Exception as e: logging.error(f"Error inesperado orden: {e}")
    return None

def get_ai_trading_signal(market_data_summary_for_ai, current_price_val, rsi_val=None, sma20_val=None, sma50_val=None, klines_summary_str=None):
    FORZAR_SEÑAL_PARA_PRUEBA = None
    if FORZAR_SEÑAL_PARA_PRUEBA:
        logging.warning(f"SEÑAL IA FORZADA: {FORZAR_SEÑAL_PARA_PRUEBA}")
        return FORZAR_SEÑAL_PARA_PRUEBA
    if not model_ai:
        logging.warning("Modelo IA no disponible. Usando simulación.")
        sim_resp = ["BUY","HOLD","SELL","HOLD"]; s_idx = getattr(get_ai_trading_signal, "c", 0) % len(sim_resp)
        get_ai_trading_signal.c = s_idx + 1; sig_txt = sim_resp[s_idx]
        logging.info(f"Respuesta SIMULADA IA: {sig_txt}"); return sig_txt
    try:
        prompt_parts = [
            f"Eres analista experto en trading de {SYMBOL_EXCHANGE} en Binance.",
            "Objetivo: swing trading corto plazo (horas-días), buena relación riesgo/beneficio.",
            "Prioriza capital; si no hay señal clara, HOLD.\n", "Estado Actual General:",
            market_data_summary_for_ai, "\nDatos de Mercado Adicionales:",
            f"- Precio Actual {BASE_ASSET}: {current_price_val:.2f} {QUOTE_ASSET}"
        ]
        if klines_summary_str: prompt_parts.append(f"- Velas Recientes: {klines_summary_str}")
        if rsi_val is not None: prompt_parts.append(f"- RSI(14): {rsi_val:.2f}")
        if sma20_val is not None: prompt_parts.append(f"- SMA20: {sma20_val:.2f}")
        if sma50_val is not None: prompt_parts.append(f"- SMA50: {sma50_val:.2f}")
        if sma20_val and sma50_val and current_price_val:
            rel_sma20 = "encima" if current_price_val > sma20_val else ("debajo" if current_price_val < sma20_val else "en")
            rel_sma50 = "encima" if current_price_val > sma50_val else ("debajo" if current_price_val < sma50_val else "en")
            prompt_parts.append(f"- Precio está {rel_sma20} SMA20 y {rel_sma50} SMA50.")
        prompt_parts.extend(["\nConsiderando TODO:", "1. Oportunidad clara de COMPRA (BUY)?",
                             "2. Oportunidad clara de VENTA (SELL)?", "3. Si no, o riesgo alto, HOLD.",
                             "Responde ÚNICAMENTE con: BUY, SELL, o HOLD."])
        prompt = "\n".join(prompt_parts)
        logging.info(f"Enviando prompt a IA ({AI_MODEL_NAME})...");
        response = model_ai.generate_content(prompt)
        sig_txt = response.text.strip().upper() if response.parts else (response.text.strip().upper() if hasattr(response, 'text') and response.text else "HOLD")
        logging.info(f"Respuesta IA: '{sig_txt}'")
        if sig_txt in ["BUY","SELL","HOLD"]: return sig_txt
        if "BUY" in sig_txt: return "BUY";
        if "SELL" in sig_txt: return "SELL"
        return "HOLD"
    except Exception as e: logging.error(f"Error señal IA: {e}"); return "HOLD"

# --- Lógica Principal del Bot ---
def run_ai_trading_bot():
    global has_position, last_buy_price, open_order_details
    
    initialize_db_table() # Inicializar tabla de BD al inicio del bot

    logging.info(f"Iniciando AI Trading Bot para {SYMBOL_EXCHANGE}...")
    si_data = get_binance_symbol_info(SYMBOL_EXCHANGE)
    if not si_data: logging.error(f"No info para {SYMBOL_EXCHANGE}. Saliendo."); return

    while True:
        # Preparar diccionario de datos para la BD, usando nombres de columna de DB_COLUMN_ORDER
        db_data_this_cycle = {col: None for col in DB_COLUMN_ORDER}
        db_data_this_cycle["timestamp"] = datetime.now() # psycopg2 maneja objetos datetime
        db_data_this_cycle["simbolo"] = SYMBOL_EXCHANGE
        
        order_action_this_cycle = False

        try:
            bal_base_ant = get_binance_asset_balance(BASE_ASSET); bal_qt_ant = get_binance_asset_balance(QUOTE_ASSET)
            # Estos son los estados ANTES de cualquier acción del ciclo, se actualizarán para el log DESPUÉS de las acciones
            
            log_header = (
                f"--- Nuevo Ciclo | Pos: {'Sí' if has_position else 'No'}, Ult.Compra: {last_buy_price if has_position else 'N/A'} | "
                f"Bal {BASE_ASSET}: {bal_base_ant:.8f}, Bal {QUOTE_ASSET}: {bal_qt_ant:.4f} | "
                f"Orden Abierta: {open_order_details['orderId'] if open_order_details else 'No'} ---"
            )
            logging.info(log_header)
            
            if open_order_details:
                # ... (Lógica de GESTIONAR ORDEN ABIERTA como estaba, actualizando db_data_this_cycle) ...
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
                            has_position = True; last_buy_price = avg_filled_price
                            logging.info(f"COMPRA COMPLETADA (previa): {exec_qty} {BASE_ASSET} a ~{last_buy_price:.4f}.")
                            db_data_this_cycle["accion_bot"] = "COMPRA_LLENADA_PREVIA"
                        elif open_order_details['side'] == 'SELL':
                            gn_ls_op = (avg_filled_price - last_buy_price) * exec_qty
                            logging.info(f"VENTA COMPLETADA (previa): {exec_qty} {BASE_ASSET} a ~{avg_filled_price:.4f}. G/P: {gn_ls_op:.4f}.")
                            db_data_this_cycle.update({"accion_bot": "VENTA_LLENADA_PREVIA", "ganancia_perdida_operacion_usdt": gn_ls_op})
                            has_position = False; last_buy_price = decimal.Decimal('0.0')
                        open_order_details = None
                    elif order_status_info['status'] in [Client.ORDER_STATUS_CANCELED, Client.ORDER_STATUS_EXPIRED, Client.ORDER_STATUS_REJECTED]:
                        logging.warning(f"Orden {open_order_details['orderId']} no activa. Estado: {order_status_info['status']}")
                        db_data_this_cycle.update({"accion_bot":f"ORDEN_FALLIDA_PREVIA ({order_status_info['status']})", "notas_adicionales": f"OrderID: {open_order_details['orderId']}"})
                        open_order_details = None
                    else: # Sigue NEW o PARTIALLY_FILLED
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
                            logging.info(f"Orden {open_order_details['orderId']} abierta. Tiempo: {time_since_order}.")
                            db_data_this_cycle.update({"accion_bot":"ESPERANDO_ORDEN_ABIERTA", "notas_adicionales": f"OrderID: {open_order_details['orderId']}"})
                except Exception as e_get_order:
                    logging.error(f"Error verificando orden {open_order_details['orderId']}: {e_get_order}")
                    db_data_this_cycle.update({"accion_bot":"ERROR_VERIFICAR_ORDEN", "notas_adicionales": f"ID: {open_order_details['orderId']}, Err: {e_get_order}"})

            if not open_order_details and not order_action_this_cycle:
                # ... (Lógica de obtener precio, datos IA, señal IA como estaba, actualizando db_data_this_cycle) ...
                mkt_data_bnc = get_current_price_from_binance(SYMBOL_EXCHANGE)
                if not mkt_data_bnc or "current_price" not in mkt_data_bnc:
                    db_data_this_cycle.update({"accion_bot":"ERROR_PRECIO", "notas_adicionales":"No se pudo obtener precio."})
                    log_to_db(db_data_this_cycle); time.sleep(CHECK_INTERVAL); continue
                cur_price = mkt_data_bnc["current_price"]
                logging.info(f"Precio actual {SYMBOL_EXCHANGE}: {cur_price:.4f} {QUOTE_ASSET}.")
                rsi_actual, sma20_actual, sma50_actual, klines_str_summary = None, None, None, None
                mkt_sum_ai = f"Precio actual {BASE_ASSET}: {cur_price:.4f} {QUOTE_ASSET}.\n"
                if has_position:
                    pl_pct = ((cur_price-last_buy_price)/last_buy_price)*100 if last_buy_price > 0 else decimal.Decimal('0')
                    mkt_sum_ai += f"Tengo {BASE_ASSET} comprado a {last_buy_price:.4f}. G/P no realizada: {pl_pct:.2f}%.\n"
                else: mkt_sum_ai += "No tengo posición abierta.\n"
                ai_sig = get_ai_trading_signal(mkt_sum_ai, cur_price, rsi_actual, sma20_actual, sma50_actual, klines_str_summary)
                logging.info(f"Señal IA: {ai_sig}"); db_data_this_cycle.update({"tipo_orden_ia":ai_sig, "respuesta_ia_completa":ai_sig})

                if not has_position and ai_sig == "BUY":
                    # ... (Lógica de COMPRA como estaba, actualizando db_data_this_cycle y open_order_details si es NEW) ...
                    db_data_this_cycle["accion_bot"] = "INTENTO_COMPRA"; logging.info(f"IA: COMPRAR {ORDER_AMOUNT_BASE} {BASE_ASSET}...")
                    order_res = place_order_on_binance(si_data, ORDER_AMOUNT_BASE, cur_price, "BUY") # Usar cur_price o precio maker
                    if order_res:
                        if order_res.get('status') == Client.ORDER_STATUS_FILLED:
                            has_position = True; exec_qty = decimal.Decimal(order_res.get('executedQty','0'))
                            cum_qt_qty = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                            last_buy_price = cum_qt_qty/exec_qty if exec_qty > 0 else decimal.Decimal(order_res.get('price',str(cur_price)))
                            logging.info(f"COMPRA LLENADA INMEDIATAMENTE: {exec_qty} {BASE_ASSET} a ~{last_buy_price:.4f}. ID:{order_res.get('orderId')}")
                            db_data_this_cycle.update({"accion_bot":"COMPRA_EJECUTADA", "precio_ejecutado":last_buy_price,
                                                    "cantidad_base_ejecutada":exec_qty, "costo_total_usdt":cum_qt_qty})
                        elif order_res.get('status') == Client.ORDER_STATUS_NEW or order_res.get('status') == Client.ORDER_STATUS_PARTIALLY_FILLED:
                            logging.info(f"Orden COMPRA {order_res['orderId']} colocada, estado: {order_res['status']}. Monitoreando...")
                            open_order_details = {'orderId': order_res['orderId'], 'side': 'BUY', 'price': decimal.Decimal(order_res['price']), 
                                                  'qty': decimal.Decimal(order_res['origQty']), 'timestamp': datetime.now()}
                            db_data_this_cycle.update({"accion_bot":"COMPRA_ORDEN_ABIERTA", "orderid_abierta": str(order_res['orderId'])})
                        else: logging.warning(f"Orden compra no completada ni abierta, estado: {order_res.get('status')}")
                        db_data_this_cycle.update({"notas_adicionales":f"Resp orden compra: {order_res.get('status')}"})
                    else: db_data_this_cycle.update({"accion_bot":"FALLO_COMPRA_API", "notas_adicionales":"place_order_on_binance devolvió None"})

                elif has_position and ai_sig == "SELL":
                    # ... (Lógica de VENTA como estaba, actualizando db_data_this_cycle y open_order_details si es NEW) ...
                    db_data_this_cycle["accion_bot"] = "INTENTO_VENTA"
                    cur_pl_pct = ((cur_price-last_buy_price)/last_buy_price)*100 if last_buy_price > 0 else decimal.Decimal('-100')
                    logging.info(f"IA: VENDER. G/P actual: {cur_pl_pct:.2f}%. Min req: {MIN_PROFIT_PERCENT_FOR_AI_SELL}%.")
                    if cur_price > last_buy_price and cur_pl_pct >= MIN_PROFIT_PERCENT_FOR_AI_SELL:
                        logging.info(f"Cond. venta OK. Vendiendo {ORDER_AMOUNT_BASE} {BASE_ASSET}...")
                        order_res = place_order_on_binance(si_data, ORDER_AMOUNT_BASE, cur_price, "SELL")
                        if order_res:
                            if order_res.get('status') == Client.ORDER_STATUS_FILLED:
                                exec_qty_s = decimal.Decimal(order_res.get('executedQty','0'))
                                cum_qt_qty_s = decimal.Decimal(order_res.get('cummulativeQuoteQty','0'))
                                avg_s_price = cum_qt_qty_s/exec_qty_s if exec_qty_s > 0 else decimal.Decimal(order_res.get('price',str(cur_price)))
                                gn_ls_op = (avg_s_price-last_buy_price)*exec_qty_s
                                logging.info(f"VENTA LLENADA INMEDIATAMENTE: {exec_qty_s} {BASE_ASSET} a ~{avg_s_price:.4f}. G/P: {gn_ls_op:.4f}. ID:{order_res.get('orderId')}")
                                db_data_this_cycle.update({"accion_bot":"VENTA_EJECUTADA", "precio_ejecutado":avg_s_price,
                                                        "cantidad_base_ejecutada":exec_qty_s, "costo_total_usdt":cum_qt_qty_s,
                                                        "ganancia_perdida_operacion_usdt":gn_ls_op})
                                has_position=False; last_buy_price=decimal.Decimal('0.0')
                            elif order_res.get('status') == Client.ORDER_STATUS_NEW or order_res.get('status') == Client.ORDER_STATUS_PARTIALLY_FILLED:
                                logging.info(f"Orden VENTA {order_res['orderId']} colocada, estado: {order_res['status']}. Monitoreando...")
                                open_order_details = {'orderId': order_res['orderId'], 'side': 'SELL', 'price': decimal.Decimal(order_res['price']), 
                                                      'qty': decimal.Decimal(order_res['origQty']), 'timestamp': datetime.now()}
                                db_data_this_cycle.update({"accion_bot":"VENTA_ORDEN_ABIERTA", "orderid_abierta": str(order_res['orderId'])})
                            else: logging.warning(f"Orden venta no completada ni abierta, estado: {order_res.get('status')}")
                            db_data_this_cycle.update({"notas_adicionales":f"Resp orden venta: {order_res.get('status')}"})
                        else: db_data_this_cycle.update({"accion_bot":"FALLO_VENTA_API", "notas_adicionales":"place_order_on_binance devolvió None"})
                    else: 
                        db_data_this_cycle["accion_bot"]="VENTA_NO_CUMPLE_COND"
                        db_data_this_cycle["notas_adicionales"]=f"Precio act {cur_price:.4f}, Compra {last_buy_price:.4f}"
                
                elif ai_sig == "HOLD": db_data_this_cycle["accion_bot"]="HOLD_IA"; logging.info("IA: ESPERAR.")
                else: db_data_this_cycle["accion_bot"]=f"SEÑAL_IA_DESCONOCIDA ({ai_sig})"; logging.warning(f"Señal IA no reconocida '{ai_sig}'.")

            # Actualizar datos finales para BD después de todas las acciones del ciclo
            bal_base_desp = get_binance_asset_balance(BASE_ASSET); bal_qt_desp = get_binance_asset_balance(QUOTE_ASSET)
            db_data_this_cycle.update({
                "tiene_posicion_despues": has_position, # bool
                "precio_ultima_compra_despues": last_buy_price if has_position else None, # Decimal o None
                "balance_base_despues": bal_base_desp, # Decimal
                "balance_quote_despues": bal_qt_desp, # Decimal
                "orderid_abierta": str(open_order_details['orderId']) if open_order_details else None
            })
            # Asegurarse de que los campos no asignados explícitamente (ej. si no hubo trade) tengan un valor por defecto
            for col in DB_COLUMN_ORDER:
                if col not in db_data_this_cycle:
                    db_data_this_cycle[col] = None # O un string vacío si la columna es VARCHAR/TEXT

            log_to_db(db_data_this_cycle)

        except KeyboardInterrupt: logging.info("Bot detenido."); break
        except Exception as e:
            logging.error(f"Error catastrófico: {e}", exc_info=True)
            db_error_data = {col: None for col in DB_COLUMN_ORDER}
            db_error_data["timestamp"] = datetime.now()
            db_error_data["accion_bot"] = "ERROR_CATASTROFICO"
            db_error_data["simbolo"] = SYMBOL_EXCHANGE
            db_error_data["notas_adicionales"] = str(e)
            log_to_db(db_error_data)
            time.sleep(60)
        
        logging.info(f"Esperando {CHECK_INTERVAL} segundos..."); time.sleep(CHECK_INTERVAL)

# ... (if __name__ == "__main__": como estaba) ...
if __name__ == "__main__":
    if not DATABASE_URL: logging.error("DATABASE_URL no configurada. El logging a BD no funcionará.")
    if not GOOGLE_AI_API_KEY: logging.warning("GOOGLE_AI_API_KEY no configurada. IA usará simulación.")
    if not BINANCE_API_KEY or not BINANCE_API_SECRET: logging.error("Claves API Binance no configuradas. Saliendo.")
    elif (not USE_TESTNET) and ("PROD" not in (BINANCE_API_KEY or "") and "PROD" not in (BINANCE_API_SECRET or "")):
         logging.warning("USE_TESTNET es False pero claves API no parecen de PRODUCCIÓN. Verifica.")
    
    if BINANCE_API_KEY and BINANCE_API_SECRET : run_ai_trading_bot()
    else: logging.error("Bot no se ejecutará por falta de claves API de Binance.")

