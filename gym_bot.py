import asyncio
import os
import datetime as dt
from datetime import datetime, timedelta
import pytz
import aiohttp
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv
import json

# --- CARGAR SECRETOS ---
load_dotenv()
TOKEN = os.getenv('TOKEN')
CHAT_ID = int(os.getenv('CHAT_ID'))
RUTINAS_ORDEN = ["Lunes", "Martes", "Jueves", "Viernes"]

# --- CONFIGURACIÓN ENTORNO MÓVIL ---
tz = pytz.timezone('America/Argentina/Buenos_Aires')
DIAS_ACTIVOS = [0, 1, 2, 3, 4, 5] 
DIAS_OFF = [6] 
BLACKLIST_ESTABILIDAD = ["EL", "BC", "PAB"]
SESIONES_PARA_ALERTA = 3

# Filtro de Seguridad estricto: El bot SOLO escuchará a tu CHAT_ID
FiltroAdmin = filters.User(user_id=CHAT_ID)

# Memoria volátil para la función "Deshacer"
ultima_carga_cache = {"activa": False, "fila": 2, "id_ejer": ""}

# --- CONFIGURACIÓN GSPREAD ASINCRONO ---
def get_creds():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    return ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", scope)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

async def obtener_nivel_bateria():
    """Llama a la API de Termux para leer la batería"""
    try:
        # Ejecuta el comando de sistema de Termux
        process = await asyncio.create_subprocess_shell(
            'termux-battery-status',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if stdout:
            res = json.loads(stdout.decode())
            return res.get('percentage', 100)
    except Exception as e:
        print(f"Error leyendo batería: {e}")
    return 100

async def verificar_estado_servidor(context: ContextTypes.DEFAULT_TYPE):
    """Revisa la batería y envía alerta si es baja"""
    nivel = await obtener_nivel_bateria()
    if nivel <= 20: # Umbral de alerta
        await context.bot.send_message(
            chat_id=CHAT_ID, 
            text=f"⚠️ **ALERTA DE ENERGÍA**\nEl servidor (celular) tiene {nivel}% de batería. Conéctalo pronto.",
            parse_mode=ParseMode.MARKDOWN
        )

# --- FUNCIONES NÚCLEO ASÍNCRONAS ---
async def obtener_estado(agcm_client):
    try:
        client = await agcm_client.authorize()
        sheet = await client.open("Gym_Log")
        ws = await sheet.worksheet("Estado")
        valores = await ws.get_all_values()
        if len(valores) > 1:
            val = valores[1]
            return {"progreso": int(val[0]), "racha": int(val[1]), "fecha": val[2]}
    except Exception as e:
        print(f"Error en estado: {e}")
    return {"progreso": 0, "racha": 0, "fecha": ""}

async def actualizar_estado(agcm_client, progreso, racha):
    try:
        client = await agcm_client.authorize()
        sheet = await client.open("Gym_Log")
        ws = await sheet.worksheet("Estado")
        fecha_hoy = datetime.now(tz).strftime("%d/%m/%Y")
        await ws.update(range_name='A2:C2', values=[[progreso, racha, fecha_hoy]])
    except Exception as e:
        print(f"Error actualización estado: {e}")

async def registrar_asistencia(agcm_client, estado_actividad):
    try:
        client = await agcm_client.authorize()
        sheet = await client.open("Gym_Log")
        ws_asist = await sheet.worksheet("Asistencia")
        hoy = datetime.now(tz).strftime("%d/%m/%Y")
        
        # Leemos las fechas para no duplicar registro hoy
        col_fechas = await ws_asist.col_values(1)
        if hoy in col_fechas: 
            return False 

        dias_es = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        dia_nombre = dias_es[datetime.now(tz).weekday()]
        
        # Obtenemos el estado para el reporte de ciclo
        estado = await obtener_estado(agcm_client)
        
        await ws_asist.insert_row([hoy, dia_nombre, estado_actividad, f"{estado['progreso'] + 1}/4"], 2)
        
        return True
    except Exception as e:
        print(f"Error en asistencia: {e}")
        return False

async def obtener_reporte_clima_detallado():
    url = "https://api.open-meteo.com/v1/forecast?latitude=-34.78&longitude=-58.38&current=temperature_2m,weather_code&hourly=precipitation_probability&timezone=America%2FArgentina%2FBuenos_Aires&forecast_days=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    temp, code = data['current']['temperature_2m'], data['current']['weather_code']
                    probs_lluvia = data['hourly']['precipitation_probability'][6:18]
                    max_prob = max(probs_lluvia)
                    emoji = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️"}.get(code, "☁️")
                    res = f"{emoji} **{temp}°C** en Temperley"
                    if max_prob > 30:
                        hora_pico = 6 + probs_lluvia.index(max_prob)
                        res += f"\n☁️ Lluvia: **{max_prob}%** (Pico {hora_pico}:00hs)"
                    return res
    except Exception as e:
        print(f"Error clima: {e}")
    return "☁️ Clima no disponible"

async def obtener_rutina_formateada(agcm_client, dia_forzado=None):
    estado = await obtener_estado(agcm_client)
    dia_nombre = dia_forzado if dia_forzado else RUTINAS_ORDEN[estado["progreso"]]
    try:
        client = await agcm_client.authorize()
        sheet = await client.open("Gym_Log")
        ws_rutinas = await sheet.worksheet("Rutinas")
        ws_ejercicios = await sheet.worksheet("Ejercicios")
        
        rutinas_data = await ws_rutinas.get_all_values()
        ejercicios_data = await ws_ejercicios.get_all_values()
        
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

async def obtener_insights_estabilidad(agcm_client, lista_ids):
    try:
        if not lista_ids: return None
        client = await agcm_client.authorize()
        sheet = await client.open("Gym_Log")
        log_ws = await sheet.worksheet("Log")
        
        # Optimización: Solo pedimos las primeras 150 filas (donde están las más recientes si insertamos en fila 2)
        rango_datos = await log_ws.get('A1:G150') 
        estables = []
        
        for eid in lista_ids:
            if eid in BLACKLIST_ESTABILIDAD: continue
            hist_ejer = [f for f in rango_datos if len(f) > 2 and f[1].upper() == eid]
            if len(hist_ejer) >= SESIONES_PARA_ALERTA:
                ultimos_pesos = [float(f[2].replace(',', '.')) for f in hist_ejer[:SESIONES_PARA_ALERTA]]
                if len(set(ultimos_pesos)) == 1: estables.append(eid)
                
        if estables:
            res = "⚖️ **REPORTE DE ESTABILIDAD**\n" + "─" * 15 + "\n"
            for e in estables: res += f"• `{e}`\n"
            return res + "\n*¿Toca subir hoy?*"
        return None
    except: return None

# --- HANDLERS DE TELEGRAM ---
async def mostrar_rutina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dia = context.args[0] if context.args else None
    res, ids = await obtener_rutina_formateada(agcm, dia)
    if res:
        clima = await obtener_reporte_clima_detallado()
        await update.message.reply_text(clima, parse_mode=ParseMode.MARKDOWN)
        insights = await obtener_insights_estabilidad(agcm, ids)
        if insights: await update.message.reply_text(insights, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text(res, parse_mode=ParseMode.MARKDOWN)

async def procesar_mensaje_peso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ultima_carga_cache
    datos = update.message.text.split()
    if len(datos) >= 2:
        id_ejer = datos[0].upper()
        try:
            peso_hoy = float(datos[1].replace(',', '.'))
            client = await agcm.authorize()
            sheet = await client.open("Gym_Log")
            ejer_ws = await sheet.worksheet("Ejercicios")
            lista_ejer = await ejer_ws.get_all_values()
            
            fila_data = next((f for f in lista_ejer if f[0].upper() == id_ejer), None)
            
            if fila_data:
                reps = int(datos[2]) if len(datos) >= 3 else int(fila_data[3])
                series = int(datos[3]) if len(datos) >= 4 else int(fila_data[4])
                volumen = peso_hoy * reps * series
                
                # 1. Insertamos en el Log siempre en la fila 2 (la más reciente arriba)
                log_ws = await sheet.worksheet("Log")
                await log_ws.insert_row([datetime.now(tz).strftime("%d/%m/%Y %H:%M"), id_ejer, peso_hoy, reps, series, volumen, "Telegram"], 2)
                
                # Guardamos en caché para posible "Deshacer"
                ultima_carga_cache = {"activa": True, "fila": 2, "id_ejer": id_ejer}
                
                # 2. Actualizamos la ficha de ejercicios
                idx = lista_ejer.index(fila_data) + 1
                await ejer_ws.update_cell(idx, 3, peso_hoy)

                # 3. Lógica de auto-avance
                fue_primera = await registrar_asistencia(agcm, "GYM")
                est = await obtener_estado(agcm)
                fecha_hoy_str = datetime.now(tz).strftime("%d/%m/%Y")

                if fue_primera or est["fecha"] != fecha_hoy_str:
                    p, r = est["progreso"] + 1, est["racha"]
                    if p > 3: p, r = 0, r + 1
                    await actualizar_estado(agcm, p, r)
                
                # Botón de Deshacer
                kb = [[InlineKeyboardButton("↩️ Deshacer carga", callback_data='deshacer_peso')]]
                markup = InlineKeyboardMarkup(kb)
                
                await update.message.reply_text(
                    f"✅ **{id_ejer}**: **{peso_hoy}kg**\n📊 {series}x{reps} | Vol: **{volumen}kg**", 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup
                )
        except Exception as e:
            await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Error guardando peso: {e}")

async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ultima_carga_cache
    query = update.callback_query
    await query.answer()
    
    if query.data == 'deshacer_peso':
        if ultima_carga_cache["activa"]:
            try:
                client = await agcm.authorize()
                sheet = await client.open("Gym_Log")
                log_ws = await sheet.worksheet("Log")
                # Borra la última fila insertada
                await log_ws.delete_rows(ultima_carga_cache["fila"])
                ultima_carga_cache["activa"] = False
                await query.edit_message_text(f"🗑️ Carga de **{ultima_carga_cache['id_ejer']}** eliminada del Log.", parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await query.edit_message_text(f"❌ Error al deshacer: {e}")
        else:
            await query.edit_message_text("Expirado o ya deshecho.")
        return

    opcion = query.data.replace('asist_', '')
    exito = await registrar_asistencia(agcm, opcion)
    
    if exito:
        if opcion == "GYM":
            estado = await obtener_estado(agcm)
            p, r = estado["progreso"] + 1, estado["racha"]
            if p > 3: p, r = 0, r + 1
            await actualizar_estado(agcm, p, r)
        await query.edit_message_text(f"✅ Asistencia registrada: **{opcion}**", parse_mode=ParseMode.MARKDOWN)
    else:
        # Mensaje de error si ya existe registro hoy
        await query.edit_message_text(f"⚠️ Ya existe un registro de asistencia para hoy. No se realizaron cambios.", parse_mode=ParseMode.MARKDOWN)

async def mostrar_heatmap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = await agcm.authorize()
        sheet = await client.open("Gym_Log")
        ws_asist = await sheet.worksheet("Asistencia")
        datos = await ws_asist.get('A1:C35') # Últimos registros
        
        dic_asistencia = {fila[0]: fila[2] for fila in datos if len(fila) >= 3}
        
        hoy = datetime.now(tz)
        heatmap_str = "📊 **TU DISCIPLINA (Últimos 28 días)**\n\n"
        
        for i in range(27, -1, -1):
            dia_eval = (hoy - timedelta(days=i)).strftime("%d/%m/%Y")
            estado = dic_asistencia.get(dia_eval, "N/A")
            
            if estado == "GYM": emoji = "🟩"
            elif estado == "FALTÉ": emoji = "🟥"
            elif estado in ["CARDIO", "EXTERNO"]: emoji = "🟨"
            else: emoji = "⬜"
            
            heatmap_str += emoji + (" " if (i % 7 != 0) else "\n")
            
        heatmap_str += "\n*(🟩: Gym | 🟥: Falta | 🟨: Cardio | ⬜: Nada)*"
        await update.message.reply_text(heatmap_str, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Error generando heatmap: {e}")

# --- TAREAS DE FONDO Y ALARMAS ---
async def rafaga_mensajes(bot, ids_rutina):
    clima = await obtener_reporte_clima_detallado()
    await bot.send_message(chat_id=CHAT_ID, text=clima, parse_mode=ParseMode.MARKDOWN)
    
    kb = [[InlineKeyboardButton("GYM", callback_data='asist_GYM'), InlineKeyboardButton("CARDIO", callback_data='asist_CARDIO')], 
          [InlineKeyboardButton("EXTERNO", callback_data='asist_EXTERNO'), InlineKeyboardButton("FALTÉ", callback_data='asist_FALTÉ')]]
    await bot.send_message(chat_id=CHAT_ID, text="¿Cuál es el plan para hoy?", reply_markup=InlineKeyboardMarkup(kb))
    
    insights = await obtener_insights_estabilidad(agcm, ids_rutina)
    if insights: await bot.send_message(chat_id=CHAT_ID, text=insights, parse_mode=ParseMode.MARKDOWN)
    
    res, _ = await obtener_rutina_formateada(agcm)
    if res: await bot.send_message(chat_id=CHAT_ID, text=res, parse_mode=ParseMode.MARKDOWN)

async def alarma_5am(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now(tz).weekday() in DIAS_OFF: return 
    _, ids = await obtener_rutina_formateada(agcm)
    await rafaga_mensajes(context.bot, ids)

async def test_completo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Forzamos escaneo de batería primero
    nivel = await obtener_nivel_bateria()
    await update.message.reply_text(f"🔋 Estado del servidor: {nivel}% de batería.")
    # Luego disparamos la ráfaga normal
    await alarma_5am(context)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()

    # Tareas programadas
    app.job_queue.run_daily(alarma_5am, time=dt.time(hour=5, minute=0, tzinfo=tz))
    app.job_queue.run_repeating(verificar_estado_servidor, interval=3600, first=10) 

    # Handlers (Solo responden si el mensaje viene de tu CHAT_ID)
    app.add_handler(CommandHandler("rutina", mostrar_rutina, filters=FiltroAdmin))
    app.add_handler(CommandHandler("heatmap", mostrar_heatmap, filters=FiltroAdmin))
    
    app.add_handler(CommandHandler("test", test_completo, filters=FiltroAdmin)) 
    
    app.add_handler(CallbackQueryHandler(manejar_botones)) # Los botones asumen el chat_id del mensaje original
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & FiltroAdmin, procesar_mensaje_peso))

    print("🤖 GymBot V1.1 (Termux Asíncrono) Operativo y Blindado.")
    app.run_polling(drop_pending_updates=True)