import os
import re
import time
import datetime
from io import BytesIO
import requests
from bs4 import BeautifulSoup
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
# MOTOR DE EXTRACCIÓN Y LIMPIEZA HTML
# ---------------------------------------------------------
def limpiar_html(contenido_html):
    """Elimina ruido estructural y extrae todo el texto útil de la ficha."""
    soup = BeautifulSoup(contenido_html, 'html.parser')

    for etiqueta in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        etiqueta.decompose()

    return soup.get_text(separator='\n', strip=True)

def extraer_texto_url(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "es-MX,es;q=0.9"
        }
        respuesta = requests.get(url, headers=headers, timeout=20)
        respuesta.raise_for_status()
        return limpiar_html(respuesta.text)
    except Exception as e:
        st.error(f"Error al leer la URL del portal estatal: {e}")
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
    """Extrae el nombre oficial del trámite o servicio desde la salida de la Fase 1."""
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
    """Convierte **negritas** y `código inline` a formato nativo de Word."""
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
    """Convierte bloques de tabla Markdown (| a | b |) en tablas reales de Word."""
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
    """Inserta el código PlantUML limpio y aislado para evitar fugas de estilo."""
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Pt(12)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(codigo_uml.strip())
    run.font.name = 'Courier New'
    run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(30, 30, 30)

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
    ("URL directa del Catálogo Jalisco", "Subir archivo HTML de la Ficha (Respaldo si tiene pestañas dinámicas)")
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
    st.caption("💡 Si la página carga pestañas con JavaScript y la URL directa extrae texto incompleto, abre la ficha en tu navegador, presiona **Cmd + S** (o Ctrl + S), guárdala como **HTML** y súbela aquí.")
    archivo_html = st.file_uploader("Arrastra aquí el archivo .html guardado", type=["html", "htm"])

if st.button("Ejecutar Arquitectura Multi-Agente", type="primary"):
    if modo == "URL directa del Catálogo Jalisco":
        if not url_input or not url_input.startswith("http"):
            st.warning("⚠️ Por favor, ingresa una URL válida que inicie con http:// o https://")
            st.stop()
        with st.spinner("Extrayendo información del Catálogo Estatal de Jalisco..."):
            texto_crudo = extraer_texto_url(url_input)
    else:
        if not archivo_html:
            st.warning("⚠️ Por favor, sube el archivo .html de la ficha.")
            st.stop()
        texto_crudo = extraer_texto_archivo_html(archivo_html)

    if not texto_crudo or len(texto_crudo.strip()) < 100:
        st.warning("⚠️ No se pudo extraer contenido suficiente. Si la página carga contenido dinámico con JavaScript, prueba usando la opción de subir el archivo HTML.")
    else:
        with st.expander("👀 Ver texto extraído de la Ficha (Debug)"):
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
                        nombre_archivo = re.sub(r'[^\w\s-]', '', nombre_tramite).strip().replace(' ', '_')

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