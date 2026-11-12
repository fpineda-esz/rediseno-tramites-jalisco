import os
import re
import time
import datetime
from io import BytesIO
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import streamlit as st
from docx import Document
from docx.shared import Pt, RGBColor
import google.generativeai as genai
from dotenv import load_dotenv

# ---------------------------------------------------------
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ---------------------------------------------------------
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

if API_KEY:
    genai.configure(api_key=API_KEY)
else:
    st.error("🚨 Error crítico: No se encontró GOOGLE_API_KEY en las variables de entorno.")
    st.stop()

MODELO_ROBUSTO = "gemini-2.5-flash"

# ---------------------------------------------------------
# CARGA MODULAR DE PROMPTS (.md)
# ---------------------------------------------------------
def cargar_prompt(ruta_archivo):
    try:
        with open(ruta_archivo, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        st.error(f"🚨 No se encontró el archivo de prompt: {ruta_archivo}")
        st.stop()

PROMPT_SKILL_1 = cargar_prompt("prompts/skill_1_auditor.md")
PROMPT_SKILL_2 = cargar_prompt("prompts/skill_2_arquitecto.md")
PROMPT_SKILL_3 = cargar_prompt("prompts/skill_3_ingeniero.md")

# ---------------------------------------------------------
# MOTOR DE EXTRACCIÓN DINÁMICA (PLAYWRIGHT + HTML)
# ---------------------------------------------------------
def limpiar_html(contenido_html):
    """Elimina ruido estructural y extrae el texto renderizado."""
    soup = BeautifulSoup(contenido_html, 'html.parser')

    for etiqueta in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        etiqueta.decompose()

    return soup.get_text(separator='\n', strip=True)

def combinar_instantaneas_pestanas(instantaneas):
    """Une el contenido de distintas pestañas eliminando encabezados/pies duplicados."""
    if not instantaneas:
        return ""
    if len(instantaneas) == 1:
        return instantaneas[0]

    base_lineas = instantaneas[0].split('\n')
    resultado = [instantaneas[0]]

    for snap in instantaneas[1:]:
        lineas = snap.split('\n')
        # Quitar prefijo idéntico (menú superior)
        inicio = 0
        while inicio < len(base_lineas) and inicio < len(lineas) and base_lineas[inicio] == lineas[inicio]:
            inicio += 1
        # Quitar sufijo idéntico (pie de página)
        fin_base = len(base_lineas) - 1
        fin_actual = len(lineas) - 1
        while fin_base >= inicio and fin_actual >= inicio and base_lineas[fin_base] == lineas[fin_actual]:
            fin_base -= 1
            fin_actual -= 1

        cuerpo_unico = "\n".join(lineas[inicio:fin_actual + 1]).strip()
        if cuerpo_unico and cuerpo_unico not in resultado:
            resultado.append(cuerpo_unico)

    return "\n\n--- SECCIÓN DE FICHA ---\n\n".join(resultado)

def extraer_texto_url(url):
    """Renderiza la página SPA con Chromium y recorre todas sus pestañas internas."""
    instantaneas = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="es-MX"
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            # Capturar vista inicial
            texto_inicial = limpiar_html(page.content())
            if texto_inicial:
                instantaneas.append(texto_inicial)

            # Detectar y recorrer pestañas o acordeones dinámicos del catálogo
            selectores_tabs = '[role="tab"], .nav-tabs a, .nav-tabs button, .mat-mdc-tab, .mat-tab-label, [data-toggle="tab"], [data-bs-toggle="tab"]'
            tabs = page.locator(selectores_tabs)
            total_tabs = tabs.count()

            for i in range(total_tabs):
                try:
                    tab = tabs.nth(i)
                    if tab.is_visible():
                        tab.click(timeout=3000)
                        page.wait_for_timeout(1000)
                        texto_tab = limpiar_html(page.content())
                        if texto_tab:
                            instantaneas.append(texto_tab)
                except Exception:
                    continue

            # Expandir acordeones cerrados si existen
            acordeones = page.locator('.accordion-button.collapsed, [data-bs-toggle="collapse"][aria-expanded="false"]')
            for j in range(acordeones.count()):
                try:
                    acordeon = acordeones.nth(j)
                    if acordeon.is_visible():
                        acordeon.click(timeout=2000)
                        page.wait_for_timeout(500)
                except Exception:
                    continue

            if acordeones.count() > 0:
                instantaneas.append(limpiar_html(page.content()))

            browser.close()

        return combinar_instantaneas_pestanas(instantaneas)
    except Exception as e:
        st.error(f"Error al renderizar la URL del portal estatal: {e}")
        return None

def extraer_texto_archivo_html(archivo_subido):
    try:
        raw_bytes = archivo_subido.getvalue()
        try:
            html_texto = raw_bytes.decode('utf-8')
        except UnicodeDecodeError:
            html_texto = raw_bytes.decode('latin-1')
        return limpiar_html(html_texto)
    except Exception as e:
        st.error(f"Error al procesar el archivo HTML: {e}")
        return None

# ---------------------------------------------------------
# CONEXIÓN CON IA (GEMINI)
# ---------------------------------------------------------
def consultar_gemini(system_instruction, texto_usuario):
    try:
        modelo = genai.GenerativeModel(
            model_name=MODELO_ROBUSTO,
            system_instruction=system_instruction
        )
        respuesta = modelo.generate_content(
            texto_usuario,
            generation_config=genai.types.GenerationConfig(temperature=0.1)
        )
        if respuesta.candidates and respuesta.candidates[0].content.parts:
            return respuesta.text
        else:
            st.error(f"Gemini detuvo la generación. Razón: {respuesta.candidates[0].finish_reason}")
            return None
    except Exception as e:
        st.error(f"Error de conexión con Gemini: {e}")
        return None

# ---------------------------------------------------------
# UTILIDADES DE EXTRACCIÓN Y FORMATEO PARA WORD (.docx)
# ---------------------------------------------------------
def extraer_nombre_tramite(texto_fase_1):
    patrones = [
        r'\*\*Nombre Oficial del Trámite:\*\*\s*(.+)',
        r'\*\*Nombre del Trámite:\*\*\s*(.+)',
        r'\*\*Nombre:\*\*\s*(.+)',
        r'Nombre Oficial del Trámite:\s*(.+)'
    ]
    for patron in patrones:
        match = re.search(patron, texto_fase_1, re.IGNORECASE)
        if match:
            nombre = match.group(1).strip().strip('*').strip()
            if len(nombre) > 3:
                return nombre
    return "Trámite o Servicio Estatal"

def agregar_texto_formateado(paragraph, texto):
    tokens = re.split(r'(\*\*.*?\*\*|`.*?`)', texto)
    for token in tokens:
        if not token:
            continue
        if token.startswith('**') and token.endswith('**') and len(token) > 4:
            run = paragraph.add_run(token[2:-2])
            run.bold = True
        elif token.startswith('`') and token.endswith('`') and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            run.font.name = 'Courier New'
            run.font.size = Pt(9.5)
        else:
            paragraph.add_run(token)

def renderizar_tabla_markdown(doc, lineas_tabla):
    filas_limpias = []
    for linea in lineas_tabla:
        celdas = [c.strip() for c in linea.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in celdas if c):
            continue
        if any(celdas):
            filas_limpias.append(celdas)

    if not filas_limpias:
        return

    num_cols = max(len(fila) for fila in filas_limpias)
    tabla = doc.add_table(rows=len(filas_limpias), cols=num_cols)
    tabla.style = 'Table Grid'

    for r_idx, fila in enumerate(filas_limpias):
        for c_idx in range(num_cols):
            valor = fila[c_idx] if c_idx < len(fila) else ""
            celda = tabla.cell(r_idx, c_idx)
            p = celda.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            if r_idx == 0:
                run = p.add_run(valor.replace('**', ''))
                run.bold = True
                run.font.size = Pt(9.5)
            else:
                agregar_texto_formateado(p, valor)
                for r in p.runs:
                    r.font.size = Pt(9)

    doc.add_paragraph()

def insertar_bloque_uml(doc, codigo_uml):
    """Inserta el código PlantUML preservando saltos de línea explícitos."""
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Pt(12)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)

    lineas_uml = codigo_uml.strip().split('\n')
    for idx, linea_uml in enumerate(lineas_uml):
        run = p.add_run(linea_uml)
        run.font.name = 'Courier New'
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor(30, 30, 30)
        if idx < len(lineas_uml) - 1:
            run.add_break()

def crear_documento_word(texto, nombre_tramite):
    doc = Document()

    estilo_normal = doc.styles['Normal']
    estilo_normal.font.name = 'Calibri'
    estilo_normal.font.size = Pt(11)

    # 1. PORTADA
    doc.add_heading('Ficha Técnica de Rediseño y Digitalización de Trámite', 0)
    sub = doc.add_heading(nombre_tramite, level=1)
    sub.runs[0].font.color.rgb = RGBColor(21, 67, 96)

    meses = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
    ]
    ahora = datetime.datetime.now()
    p_fecha = doc.add_paragraph(f"Gobierno del Estado de Jalisco  |  {meses[ahora.month - 1]} {ahora.year}")
    p_fecha.runs[0].font.size = Pt(11)
    doc.add_page_break()

    # 2. ÍNDICE DE CONTENIDO
    doc.add_heading('Contenido', level=1)
    lineas = texto.split('\n')
    for linea in lineas:
        l_strip = linea.strip()
        if l_strip.startswith('### '):
            titulo = l_strip.replace('### ', '').strip()
            p_toc = doc.add_paragraph(titulo)
            p_toc.runs[0].bold = True

    doc.add_page_break()

    # 3. CUERPO DEL DOCUMENTO (PARSER MARKDOWN -> WORD)
    en_bloque_uml = False
    recien_cerro_uml = False
    buffer_uml = []
    buffer_tabla = []

    def vaciar_tabla():
        nonlocal buffer_tabla
        if buffer_tabla:
            renderizar_tabla_markdown(doc, buffer_tabla)
            buffer_tabla = []

    for linea_raw in lineas:
        linea = linea_raw.strip()

        if linea.startswith("```"):
            vaciar_tabla()
            if en_bloque_uml:
                insertar_bloque_uml(doc, "\n".join(buffer_uml))
                buffer_uml = []
                en_bloque_uml = False
                recien_cerro_uml = True
            elif recien_cerro_uml:
                recien_cerro_uml = False
            else:
                en_bloque_uml = True
                buffer_uml = []
            continue

        if linea.startswith("@startuml"):
            vaciar_tabla()
            en_bloque_uml = True
            recien_cerro_uml = False
            buffer_uml = [linea]
            continue

        if en_bloque_uml:
            buffer_uml.append(linea_raw.rstrip())
            if linea.startswith("@enduml"):
                insertar_bloque_uml(doc, "\n".join(buffer_uml))
                buffer_uml = []
                en_bloque_uml = False
                recien_cerro_uml = True
            continue

        recien_cerro_uml = False

        if linea.startswith('|') and '|' in linea[1:]:
            buffer_tabla.append(linea)
            continue
        else:
            vaciar_tabla()

        if not linea:
            continue

        if linea.startswith('### '):
            doc.add_heading(linea.replace('### ', '').strip(), level=1)
        elif linea.startswith('#### '):
            doc.add_heading(linea.replace('#### ', '').strip(), level=2)
        elif linea.startswith('* ') or linea.startswith('- '):
            es_subvineta = linea_raw.startswith('  ') or linea_raw.startswith('\t')
            estilo_lista = 'List Bullet 2' if es_subvineta else 'List Bullet'
            p = doc.add_paragraph(style=estilo_lista)
            agregar_texto_formateado(p, linea[2:].strip())
        elif re.match(r'^\d+\.\s+', linea):
            texto_sin_num = re.sub(r'^\d+\.\s+', '', linea)
            p = doc.add_paragraph(style='List Number')
            agregar_texto_formateado(p, texto_sin_num)
        else:
            p = doc.add_paragraph()
            agregar_texto_formateado(p, linea)

    vaciar_tabla()
    if en_bloque_uml and buffer_uml:
        insertar_bloque_uml(doc, "\n".join(buffer_uml))

    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer

# ---------------------------------------------------------
# INTERFAZ DE USUARIO (STREAMLIT)
# ---------------------------------------------------------
st.set_page_config(page_title="Fábrica de Rediseño | Jalisco", page_icon="⚙️", layout="centered")

st.title("⚙️ Orquestador Multi-Agente")
st.subheader("Rediseño y Digitalización de Trámites y Servicios (Gobierno del Estado de Jalisco)")

modo = st.radio(
    "Selecciona la fuente de la Ficha Informativa:",
    ("URL directa del Catálogo Jalisco", "Subir archivo HTML guardado de la Ficha")
)

texto_crudo = None
url_input = None
archivo_html = None

if modo == "URL directa del Catálogo Jalisco":
    url_input = st.text_input(
        "Pega la URL de la Ficha Informativa oficial del trámite o servicio:",
        placeholder="[https://tramites.jalisco.gob.mx/catalogo.jalisco/](https://tramites.jalisco.gob.mx/catalogo.jalisco/)..."
    )
else:
    st.caption("💡 En Chrome/Edge, guarda la página como **'Página web, completa'** para conservar el contenido renderizado por JavaScript.")
    archivo_html = st.file_uploader("Arrastra aquí el archivo .html guardado", type=["html", "htm"])

if st.button("Ejecutar Arquitectura Multi-Agente", type="primary"):
    if modo == "URL directa del Catálogo Jalisco":
        if not url_input or not url_input.startswith("http"):
            st.warning("⚠️ Por favor, ingresa una URL válida que inicie con http:// o https://")
            st.stop()
        with st.spinner("Abriendo ficha en navegador headless y recorriendo pestañas..."):
            texto_crudo = extraer_texto_url(url_input)
    else:
        if not archivo_html:
            st.warning("⚠️ Por favor, sube el archivo .html de la ficha.")
            st.stop()
        texto_crudo = extraer_texto_archivo_html(archivo_html)

    if not texto_crudo or len(texto_crudo.strip()) < 150:
        st.error("⚠️ No se pudo extraer contenido suficiente de la ficha. Verifica que la URL esté activa.")
    else:
        with st.expander("👀 Ver texto extraído completo de todas las pestañas (Debug)"):
            st.text(texto_crudo)

        progreso = st.progress(0)

        with st.status("Procesando trámite en 3 fases...", expanded=True) as status:
            st.write("🕵️‍♂️ **Fase 1:** Analizando escenario base (Auditor As-Is)...")
            input_fase_1 = f"TEXTO DEL TRÁMITE O SERVICIO A EVALUAR:\n{texto_crudo}"
            res_fase_1 = consultar_gemini(PROMPT_SKILL_1, input_fase_1)
            progreso.progress(33)
            time.sleep(1)

            if res_fase_1 and "### 1" in res_fase_1:
                nombre_tramite = extraer_nombre_tramite(res_fase_1)

                st.write("🏗️ **Fase 2:** Diseñando ecosistema To-Be (Arquitecto)...")
                input_fase_2 = f"TEXTO ORIGINAL:\n{texto_crudo}\n\n--- DIAGNÓSTICO FASE 1 ---\n{res_fase_1}"
                res_fase_2 = consultar_gemini(PROMPT_SKILL_2, input_fase_2)
                progreso.progress(66)
                time.sleep(1)

                if res_fase_2 and "### 3" in res_fase_2:
                    st.write("💻 **Fase 3:** Parametrizando tecnología (Ingeniero Tech)...")
                    input_fase_3 = f"TEXTO ORIGINAL:\n{texto_crudo}\n\n--- FASE 1 ---\n{res_fase_1}\n\n--- FASE 2 ---\n{res_fase_2}"
                    res_fase_3 = consultar_gemini(PROMPT_SKILL_3, input_fase_3)
                    progreso.progress(100)

                    if res_fase_3:
                        status.update(label="¡Procesamiento exitoso!", state="complete", expanded=False)

                        documento_final = f"{res_fase_1}\n\n{res_fase_2}\n\n{res_fase_3}"
                        archivo_word = crear_documento_word(documento_final, nombre_tramite)
                        nombre_archivo = re.sub(r'[^\w\s-]', '', nombre_tramite).strip().replace(' ', '_')[:60]

                        st.success(f"✅ Ficha Técnica generada para: **{nombre_tramite}**")
                        st.download_button(
                            label="📥 Descargar Ficha Técnica (.docx)",
                            data=archivo_word,
                            file_name=f"Ficha_Tecnica_{nombre_archivo}.docx",
                            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                        )
                    else:
                        status.update(label="Fallo en Fase 3", state="error")
                else:
                    status.update(label="Fallo en Fase 2", state="error")
            else:
                status.update(label="Fallo en Fase 1 (El modelo no generó la estructura esperada)", state="error")
