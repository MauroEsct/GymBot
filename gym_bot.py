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
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import io
import os 
from dotenv import load_dotenv

# --- CARGAR SECRETOS ---
load_dotenv()
TOKEN = os.getenv('TOKEN')
CHAT_ID = int(os.getenv('CHAT_ID'))
RUTINAS_ORDEN = ["Lunes", "Martes", "Jueves", "Viernes"]

# --- CONFIGURACIÓN MODULAR ---
DIAS_ACTIVOS = [0, 1, 2, 3, 4, 5]
DIAS_OFF = [6]
BLACKLIST_ESTABILIDAD = ["EL", "BC", "PAB"]
SESIONES_PARA_ALERTA = 3

# --- GOOGLE SHEETS ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", scope)
client = gspread.authorize(creds)
sheet = client.open("Gym_Log")

# --- FUNCIONES DE INFORMACIÓN ---
def obtener_reporte_clima():
    url = "https://api.open-meteo.com/v1/forecast?latitude=-34.78&longitude=-58.38&current=temperature_2m,weather_code&hourly=precipitation_probability&timezone=America%2FArgentina%2FBuenos_Aires&forecast_days=1"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            temp = data['current']['temperature_2m']
            code = data['current']['weather_code']
            max_prob = max(data['hourly']['precipitation_probability'][6:18])
            emoji = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️"}.get(code, "☁️")
            res = f"{emoji} **{temp}°C** en Temperley"
            if max_prob > 30: res += f"\n☁️ Lluvia: **{max_prob}%** hoy"
            return res
    except: pass
    return "☁️ Clima no disponible"

def obtener_estado():
    try:
        ws = sheet.worksheet("Estado")
        valores = ws.get_all_values()[1]
        return {"progreso": int(valores[0]), "racha": int(valores[1]), "fecha": valores[2]}
    except: return {"progreso": 0, "racha": 0, "fecha": ""}

def registrar_asistencia(estado_actividad):
    ws_asist = sheet.worksheet("Asistencia")
    hoy = datetime.now().strftime("%d/%m/%Y")
    if hoy in ws_asist.col_values(1): return False 
    est = obtener_estado()
    ws_asist.append_row([hoy, "Día", estado_actividad, f"{est['progreso']+1}/4"])
    return True

def actualizar_estado(progreso, racha):
    try: sheet.worksheet("Estado").update([[progreso, racha, datetime.now().strftime("%d/%m/%Y")]], 'A2:C2')
    except: pass

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
        ids_f = []
        for eid in lista_ids:
            eid_c = eid.strip().upper()
            if eid_c in dict_ejer:
                n, p, r, s = dict_ejer[eid_c]
                mensaje += f"`{eid_c: <4} |` **{n}** — **{s}x{r}**\n"
                if p and str(p) != '0': mensaje += f"┗━━ **{p}kg**\n"
                ids_f.append(eid_c)
        mensaje += "─" * 15 + f"\nCiclo: {estado['progreso']+1}/4 | Racha: {estado['racha']} sem\n"
        return mensaje, ids_f
    except: return None, []

def obtener_insights_estabilidad(lista_ids):
    try:
        log_ws = sheet.worksheet("Log")
        datos = log_ws.get_all_values()[1:151] 
        estables = []
        for eid in lista_ids:
            if eid in BLACKLIST_ESTABILIDAD: continue
            hist = [f for f in datos if f[1].upper() == eid]
            if len(hist) >= SESIONES_PARA_ALERTA:
                pesos = [float(f[2].replace(',', '.')) for f in hist[:SESIONES_PARA_ALERTA]]
                if len(set(pesos)) == 1: estables.append(eid)
        if estables:
            res = "⚖️ **REPORTE DE ESTABILIDAD**\n" + "─" * 15 + "\n"
            for e in estables: res += f"• `{e}`\n"
            return res + "\n*¿Toca subir hoy?*"
        return None
    except: return None

# --- MOTOR GRÁFICO SECUENCIAL ---
def generar_heatmap_secuencial(periodo='mes'):
    try:
        ws = sheet.worksheet("Asistencia")
        data = ws.get_all_values()[1:]
        hoy = datetime.now()
        secuencia = []
        limite = hoy - timedelta(days=7) if periodo == 'semana' else None
        for fila in data:
            f_dt = datetime.strptime(fila[0], "%d/%m/%Y")
            if periodo == 'mes' and f_dt.month != hoy.month: continue
            if periodo == 'semana' and f_dt < limite: continue
            if fila[2] in ["GYM", "CARDIO"]: secuencia.append(1)
            elif fila[2] == "FALTÉ": secuencia.append(-1)
        if not secuencia: return None
        cols = 7
        filas = (len(secuencia) + cols - 1) // cols
        grid = np.zeros((filas, cols))
        for i, v in enumerate(secuencia): grid[divmod(i, cols)] = v
        plt.switch_backend('Agg')
        fig, ax = plt.subplots(figsize=(cols*0.5, filas*0.5), facecolor='#121212')
        ax.set_facecolor('#121212')
        for r in range(filas):
            for c in range(cols):
                val = grid[r, c]
                if val == 0: continue
                color = '#2ecc71' if val == 1 else '#e74c3c'
                ax.add_patch(mpatches.Rectangle((c, filas-1-r), 0.85, 0.85, facecolor=color, edgecolor='none'))
        ax.set_xlim(-0.1, cols); ax.set_ylim(-0.1, filas); plt.axis('off')
        buf = io.BytesIO(); plt.savefig(buf, format='png', bbox_inches='tight', facecolor='#121212'); buf.seek(0); plt.close(fig)
        return buf
    except: return None

# --- HANDLERS ---
async def borrar_ultimo_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        log_ws = sheet.worksheet("Log")
        last_data = log_ws.row_values(2) 
        if last_data:
            log_ws.delete_rows(2)
            await update.message.reply_text(f"🗑️ **Fila eliminada:** {last_data[1]} ({last_data[2]}kg). Tus fórmulas de Excel se actualizarán solas.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("El log parece estar vacío.")
    except Exception as e:
        await update.message.reply_text(f"Error al borrar: {e}")

async def enviar_grafico_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buf = generar_heatmap_secuencial(periodo='mes')
    if buf: await update.message.reply_photo(photo=buf, caption="🔥 **Flujo de Disciplina**", parse_mode=ParseMode.MARKDOWN)

async def reporte_dominical(context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.now()
    es_ultimo = (hoy + timedelta(days=7)).month != hoy.month
    tipo = 'mes' if es_ultimo else 'semana'
    buf = generar_heatmap_secuencial(periodo=tipo)
    if buf: await context.bot.send_photo(chat_id=CHAT_ID, photo=buf, caption="🏆 Cierre de Periodo", parse_mode=ParseMode.MARKDOWN)

async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    opcion = query.data.replace('asist_', '')
    if registrar_asistencia(opcion):
        if opcion == "GYM":
            est = obtener_estado(); p, r = est["progreso"]+1, est["racha"]
            if p > 3: p, r = 0, r+1
            actualizar_estado(p, r)
        await query.edit_message_text(f"Asistencia: **{opcion}**", parse_mode=ParseMode.MARKDOWN)

async def mostrar_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dia = context.args[0] if context.args else None
    res, ids = obtener_rutina_formateada(dia)
    if res:
        await update.message.reply_text(obtener_reporte_clima(), parse_mode=ParseMode.MARKDOWN)
        ins = obtener_insights_estabilidad(ids)
        if ins: await update.message.reply_text(ins, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text(res, parse_mode=ParseMode.MARKDOWN)

async def procesar_mensaje_peso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = update.message.text.split()
    if len(datos) >= 2:
        id_e = datos[0].upper()
        try:
            peso = float(datos[1].replace(',', '.'))
            ejer_ws = sheet.worksheet("Ejercicios")
            lista = ejer_ws.get_all_values()
            f = next((x for x in lista if x[0].upper() == id_e), None)
            
            if f:
                series = int(datos[2]) if len(datos) >= 3 else int(f[4])
                reps = int(datos[3]) if len(datos) >= 4 else int(f[3])
                vol = peso * reps * series
                
                log_ws = sheet.worksheet("Log")
                log_ws.insert_row([datetime.now().strftime("%d/%m/%Y %H:%M"), id_e, peso, reps, series, vol, "Telegram"], 2)
                
                registrar_asistencia("GYM")
                await update.message.reply_text(f"✅ **{id_e}**: **{peso}kg**\n📊 **{series}x{reps}** | Volumen: **{vol}kg**", parse_mode=ParseMode.MARKDOWN)
        except: pass

async def test_5am(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 **Simulando ráfaga...**")
    await alarma_5am(context)

async def alarma_5am(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now().weekday() in DIAS_OFF: return 
    await context.bot.send_message(chat_id=CHAT_ID, text=obtener_reporte_clima(), parse_mode=ParseMode.MARKDOWN)
    kb = [[InlineKeyboardButton("GYM", callback_data='asist_GYM'), InlineKeyboardButton("CARDIO", callback_data='asist_CARDIO')], [InlineKeyboardButton("EXTERNO", callback_data='asist_EXTERNO'), InlineKeyboardButton("FALTÉ", callback_data='asist_FALTÉ')]]
    await context.bot.send_message(chat_id=CHAT_ID, text="¿Cuál es el plan para hoy?", reply_markup=InlineKeyboardMarkup(kb))
    res, ids = obtener_rutina_formateada()
    if res:
        ins = obtener_insights_estabilidad(ids)
        if ins: await context.bot.send_message(chat_id=CHAT_ID, text=ins, parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(chat_id=CHAT_ID, text=res, parse_mode=ParseMode.MARKDOWN)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    tz = pytz.timezone('America/Argentina/Buenos_Aires')
    app.job_queue.run_daily(alarma_5am, time=dt.time(hour=5, minute=0, tzinfo=tz))
    app.job_queue.run_daily(reporte_dominical, time=dt.time(hour=21, minute=0, tzinfo=tz), days=(6,))
    
    app.add_handler(CommandHandler("rutina", mostrar_rutina))
    app.add_handler(CommandHandler("grafico", enviar_grafico_manual))
    app.add_handler(CommandHandler("borrar", borrar_ultimo_log))
    app.add_handler(CommandHandler("test", test_5am))
    app.add_handler(CallbackQueryHandler(manejar_botones))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), procesar_mensaje_peso))
    
    print("🤖 GymBot V5.7 Operativo.")
    app.run_polling(drop_pending_updates=True)