import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from datetime import datetime, timedelta
import datetime as dt
import pytz
import requests
import asyncio
import os 
from dotenv import load_dotenv

# --- CARGAR SECRETOS (Esencial para Termux) ---
load_dotenv()
TOKEN = os.getenv('TOKEN')
CHAT_ID = int(os.getenv('CHAT_ID'))
RUTINAS_ORDEN = ["Lunes", "Martes", "Jueves", "Viernes"]

# --- CONFIGURACIÓN HUSO HORARIO (El arreglo para el celular) ---
tz = pytz.timezone('America/Argentina/Buenos_Aires')

# --- CONFIGURACIÓN MODULAR V4.1 ---
DIAS_ACTIVOS = [0, 1, 2, 3, 4, 5] 
DIAS_OFF = [6] 
BLACKLIST_ESTABILIDAD = ["EL", "BC", "PAB"]
SESIONES_PARA_ALERTA = 3

# --- GOOGLE SHEETS ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", scope)
client = gspread.authorize(creds)
sheet = client.open("Gym_Log")

# --- TUS FUNCIONES DE LÓGICA (RESPETADAS AL 100%) ---
def obtener_estado():
    try:
        ws = sheet.worksheet("Estado")
        valores = ws.get_all_values()[1]
        return {"progreso": int(valores[0]), "racha": int(valores[1]), "fecha": valores[2]}
    except: return {"progreso": 0, "racha": 0, "fecha": ""}

def registrar_asistencia(estado_actividad):
    ws_asist = sheet.worksheet("Asistencia")
    hoy = datetime.now(tz).strftime("%d/%m/%Y") # Usamos tz para no pifiar la fecha
    col_fechas = ws_asist.col_values(1)
    if hoy in col_fechas: return False 

    dias_es = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    dia_nombre = dias_es[datetime.now(tz).weekday()]
    estado = obtener_estado()
    ws_asist.append_row([hoy, dia_nombre, estado_actividad, f"{estado['progreso'] + 1}/4"])
    return True

def actualizar_estado(progreso, racha):
    try:
        ws = sheet.worksheet("Estado")
        fecha_hoy = datetime.now(tz).strftime("%d/%m/%Y")
        # Usamos nombres de argumentos para que sea más robusto en Termux
        ws.update(range_name='A2:C2', values=[[progreso, racha, fecha_hoy]])
        print(f"✅ Excel Actualizado: Ciclo {progreso+1}/4")
    except Exception as e:
        print(f"❌ Error actualización estado: {e}")

async def procesar_mensaje_peso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = update.message.text.split()
    if len(datos) >= 2:
        id_ejer = datos[0].upper()
        try:
            peso_hoy = float(datos[1].replace(',', '.'))
            ejer_ws = sheet.worksheet("Ejercicios")
            lista_ejer = ejer_ws.get_all_values()
            fila_data = next((f for f in lista_ejer if f[0].upper() == id_ejer), None)
            
            if fila_data:
                reps = int(datos[2]) if len(datos) >= 3 else int(fila_data[3])
                series = int(datos[3]) if len(datos) >= 4 else int(fila_data[4])
                volumen = peso_hoy * reps * series
                
                # 1. Anotamos en el Log (esto ya te funcionaba)
                sheet.worksheet("Log").insert_row([datetime.now(tz).strftime("%d/%m/%Y %H:%M"), id_ejer, peso_hoy, reps, series, volumen, "Telegram"], 2)
                
                # 2. Actualizamos la ficha de ejercicios
                idx = lista_ejer.index(fila_data) + 1
                ejer_ws.update_cell(idx, 3, peso_hoy)

                # --- 🚀 LÓGICA DE AVANCE MEJORADA ---
                # Intentamos registrar asistencia (devuelve True si es la primera vez hoy)
                fue_primera_asistencia = registrar_asistencia("GYM")
                
                # Leemos cómo está el bot actualmente
                est = obtener_estado()
                fecha_hoy_str = datetime.now(tz).strftime("%d/%m/%Y")

                # REGLA: Avanzamos si es la primera vez hoy O si la fecha del Estado quedó vieja
                if fue_primera_asistencia or est["fecha"] != fecha_hoy_str:
                    print("⚙️ Avanzando ciclo (Día nuevo detectado)...")
                    p, r = est["progreso"] + 1, est["racha"]
                    if p > 3: p, r = 0, r + 1
                    actualizar_estado(p, r)
                # ------------------------------------

                await update.message.reply_text(f"✅ **{id_ejer}**: **{peso_hoy}kg**\n📊 {series}x{reps} | Vol: **{volumen}kg**", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"Error procesando peso: {e}")

def obtener_rutina_formateada(dia_forzado=None):
    estado = obtener_estado()
    dia_nombre = dia_forzado if dia_forzado else RUTINAS_ORDEN[estado["progreso"]]
    try:
        rutinas_data = sheet.worksheet("Rutinas").get_all_values()
        ejercicios_data = sheet.worksheet("Ejercicios").get_all_values()
        lista_ids = next((f[1].split(", ") for f in rutinas_data if f[0].lower() == dia_nombre.lower()), None)
        if not lista_ids: return None, []
        dict_ejer = {f[0].upper(): (f[1], f[2], f[3], f[4]) for f in ejercicios_data if len(f) >= 5}
        mensaje = f"🗓 **{dia_nombre.upper()}**\n" + "─" * 15 + "\n"
        ids_finales = []
        for eid in lista_ids:
            eid_clean = eid.strip().upper()
            if eid_clean in dict_ejer:
                nombre, peso, reps, series = dict_ejer[eid_clean]
                nombre_clean = str(nombre).replace('_', ' ').replace('*', '')
                mensaje += f"`{eid_clean: <4} |` **{nombre_clean}** — **{series}x{reps}**\n"
                if peso and str(peso).strip() != '0': mensaje += f"┗━━ **{peso}kg**\n"
                ids_finales.append(eid_clean)
        racha_txt = f"{estado['racha']} semanas" if estado['racha'] > 0 else f"{estado['progreso']} días"
        mensaje += "─" * 15 + f"\nCiclo: {estado['progreso'] + 1}/4 | Racha: {racha_txt}\n"
        return mensaje, ids_finales
    except: return None, []

def obtener_insights_estabilidad(lista_ids):
    try:
        if not lista_ids: return None
        log_ws = sheet.worksheet("Log")
        datos_log = log_ws.get_all_values()[-150:] 
        estables = []
        for eid in lista_ids:
            if eid in BLACKLIST_ESTABILIDAD: continue
            hist_ejer = [f for f in datos_log if f[1].upper() == eid]
            if len(hist_ejer) >= SESIONES_PARA_ALERTA:
                ultimos_pesos = [float(f[2].replace(',', '.')) for f in hist_ejer[-SESIONES_PARA_ALERTA:]]
                if len(set(ultimos_pesos)) == 1: estables.append(eid)
        if estables:
            res = "⚖️ **REPORTE DE ESTABILIDAD**\n" + "─" * 15 + "\n"
            for e in estables: res += f"• `{e}`\n"
            return res + "\n*¿Toca subir hoy?*"
        return None
    except: return None

def obtener_reporte_clima_detallado():
    url = "https://api.open-meteo.com/v1/forecast?latitude=-34.78&longitude=-58.38&current=temperature_2m,weather_code&hourly=precipitation_probability&timezone=America%2FArgentina%2FBuenos_Aires&forecast_days=1"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            temp, code = data['current']['temperature_2m'], data['current']['weather_code']
            probs_lluvia = data['hourly']['precipitation_probability'][6:18]
            max_prob = max(probs_lluvia)
            emoji = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️"}.get(code, "☁️")
            res = f"{emoji} **{temp}°C** en Temperley"
            if max_prob > 30:
                hora_pico = 6 + probs_lluvia.index(max_prob)
                res += f"\n☁️ Lluvia: **{max_prob}%** (Pico {hora_pico}:00hs)"
            return res
    except: return "☁️ Clima no disponible"

# --- TUS HANDLERS ---
async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    opcion = query.data.replace('asist_', '')
    if registrar_asistencia(opcion):
        if opcion == "GYM":
            estado = obtener_estado()
            p, r = estado["progreso"] + 1, estado["racha"]
            if p > 3: p, r = 0, r + 1
            actualizar_estado(p, r)
        await query.edit_message_text(f"Asistencia registrada: **{opcion}**", parse_mode=ParseMode.MARKDOWN)

async def mostrar_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dia = context.args[0] if context.args else None
    res, ids = obtener_rutina_formateada(dia)
    if res:
        await update.message.reply_text(obtener_reporte_clima_detallado(), parse_mode=ParseMode.MARKDOWN)
        insights = obtener_insights_estabilidad(ids)
        if insights: await update.message.reply_text(insights, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text(res, parse_mode=ParseMode.MARKDOWN)

async def procesar_mensaje_peso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = update.message.text.split()
    if len(datos) >= 2:
        id_ejer = datos[0].upper()
        try:
            peso_hoy = float(datos[1].replace(',', '.'))
            ejer_ws = sheet.worksheet("Ejercicios")
            lista_ejer = ejer_ws.get_all_values()
            fila_data = next((f for f in lista_ejer if f[0].upper() == id_ejer), None)
            
            if fila_data:
                reps = int(datos[2]) if len(datos) >= 3 else int(fila_data[3])
                series = int(datos[3]) if len(datos) >= 4 else int(fila_data[4])
                volumen = peso_hoy * reps * series
                
                sheet.worksheet("Log").append_row([datetime.now(tz).strftime("%d/%m/%Y %H:%M"), id_ejer, peso_hoy, reps, series, volumen, "Telegram"], 2)
                ejer_ws.update_cell(lista_ejer.index(fila_data) + 1, 3, peso_hoy)

                # TU LÓGICA DE AUTO-AVANCE (RESPETADA)
                if registrar_asistencia("GYM"):
                    print("🚀 Primer ejercicio del día. Avanzando ciclo...")
                    est = obtener_estado()
                    p, r = est["progreso"] + 1, est["racha"]
                    if p > 3: p, r = 0, r + 1
                    actualizar_estado(p, r)
                
                await update.message.reply_text(f"✅ **{id_ejer}**: **{peso_hoy}kg**\n📊 {series}x{reps} | Vol: **{volumen}kg**", parse_mode=ParseMode.MARKDOWN)
        except: pass

async def verificar_recuperacion(context: ContextTypes.DEFAULT_TYPE):
    ahora = datetime.now(tz)
    if ahora.weekday() in DIAS_OFF: return
    ws_asist = sheet.worksheet("Asistencia")
    if ahora.strftime("%d/%m/%Y") not in ws_asist.col_values(1):
        if ahora.hour >= 5:
            print("⚡ Alarma perdida detectada. Enviando ráfaga...")
            _, ids = obtener_rutina_formateada()
            await ráfaga_mensajes(context.bot, ids)

async def ráfaga_mensajes(bot, ids_rutina):
    await bot.send_message(chat_id=CHAT_ID, text=obtener_reporte_clima_detallado(), parse_mode=ParseMode.MARKDOWN)
    kb = [[InlineKeyboardButton("GYM", callback_data='asist_GYM'), InlineKeyboardButton("CARDIO", callback_data='asist_CARDIO')], [InlineKeyboardButton("EXTERNO", callback_data='asist_EXTERNO'), InlineKeyboardButton("FALTÉ", callback_data='asist_FALTÉ')]]
    await bot.send_message(chat_id=CHAT_ID, text="¿Cuál es el plan para hoy?", reply_markup=InlineKeyboardMarkup(kb))
    insights = obtener_insights_estabilidad(ids_rutina)
    if insights: await bot.send_message(chat_id=CHAT_ID, text=insights, parse_mode=ParseMode.MARKDOWN)
    res, _ = obtener_rutina_formateada()
    if res: await bot.send_message(chat_id=CHAT_ID, text=res, parse_mode=ParseMode.MARKDOWN)

async def alarma_5am(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now(tz).weekday() in DIAS_OFF: return 
    _, ids = obtener_rutina_formateada()
    await ráfaga_mensajes(context.bot, ids)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.job_queue.run_daily(alarma_5am, time=dt.time(hour=5, minute=0, tzinfo=tz))
    app.job_queue.run_once(verificar_recuperacion, when=10) # Chequeo al arrancar

    app.add_handler(CommandHandler("rutina", mostrar_rutina))
    app.add_handler(CommandHandler("test", lambda u, c: alarma_5am(c)))
    app.add_handler(CallbackQueryHandler(manejar_botones))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), procesar_mensaje_peso))
    print("🤖 GymBot V4.1 (Tu lógica + Termux Fix) Operativo.")
    app.run_polling(drop_pending_updates=True)