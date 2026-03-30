import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. Configurar los permisos
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# 2. Cargar tus credenciales
# Asegurate de que el archivo se llame exactamente igual en tu carpeta
try:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", scope)
    client = gspread.authorize(creds)

    # 3. Abrir la planilla
    # Debe llamarse exactamente Gym_Log en Google Drive
    sheet = client.open("Gym_Log").sheet1

    # 4. Escribir una fila de prueba
    sheet.append_row(["2026-03-16", "TEST", "99", "¡Conexión exitosa desde CachyOS!"])
    
    print("✅ ¡Éxito! Revisá tu Google Sheet, debería haber una fila nueva al final.")

except Exception as e:
    print(f"❌ Error: {e}")