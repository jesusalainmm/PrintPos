from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import os
import platform
import subprocess
import re
from typing import List, Dict, Optional
import logging
from escpos.printer import Usb, Network, Dummy, Win32Raw
from escpos.exceptions import USBNotFoundError, Error
from escpos.image import EscposImage
import usb.core
import socket
import tempfile
from io import BytesIO
from PIL import Image
import html2text
import qrcode
import base64
from bs4 import BeautifulSoup

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Importación condicional de win32print
try:
    import win32print
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False
    logger.warning("win32print no disponible - algunas funciones de impresión limitadas")


app = FastAPI(
    title="PrintPOS API",
    description="API para impresión de tickets POS en impresoras térmicas",
    version="1.0.0"
)

# Configurar CORS - Permitir todos los orígenes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permitir cualquier origen
    allow_credentials=False,  # Debe ser False cuando allow_origins=["*"]
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Middleware adicional para CORS universal
@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    # Manejar solicitudes OPTIONS preflight
    if request.method == "OPTIONS":
        response = Response()
        response.headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "86400"
        })
        return response
    
    # Procesar solicitud normal
    response = await call_next(request)
    
    # Agregar headers CORS a todas las respuestas
    response.headers.update({
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS", 
        "Access-Control-Allow-Headers": "*"
    })
    
    return response

# Modelos Pydantic
class PrintRequest(BaseModel):
    printer: str
    size: str  # "80mm" o "58mm"
    html: str
    font_size: Optional[str] = 'normal'  # "normal" o "small"
    test_width: Optional[bool] = False  # Para imprimir línea de prueba de ancho
    line_spacing: Optional[str] = 'normal'  # "compact", "normal", "wide"

class VersionResponse(BaseModel):
    version: str
    name: str

class PrinterInfo(BaseModel):
    name: str
    connection_type: str
    status: str
    description: Optional[str] = None

class PrintResponse(BaseModel):
    success: bool
    message: str
    printer_used: Optional[str] = None

class StatusResponse(BaseModel):
    status: bool
    message: str

# Cargar configuración
def load_config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("ERROR: El archivo config.json no fue encontrado en la carpeta actual.")
        logger.error("config.json no encontrado")
        return None
    except json.JSONDecodeError as e:
        # print(f"ERROR: El archivo config.json tiene errores de formato: {e}")
        logger.error(f"Error al leer config.json: {e}")
        return None

config = load_config()
if not config:
    print("ERROR: No se pudo cargar la configuración. El API no se iniciará.")
    import sys
    sys.exit(1)

def print_html(printer_name, html_content, paper_size="80mm", font_size='normal', test_width=False, line_spacing='normal'):    
    """Imprimir usando escpos con impresora del sistema"""
    # print(f"Imprimir usando escpos con impresora del sistema: {printer_name} (papel: {paper_size}, fuente: {font_size})")
    try:    
        from escpos import printer    
        # Intentar usar Win32Raw si está disponible
        if WIN32_AVAILABLE:            
            try:
                p = printer.Win32Raw(printer_name)
                process_html_for_escpos(p, html_content, paper_size, font_size, test_width, line_spacing)                
                # p.text("Grácias por usar PrintPOS!")
                # p.text(str(content))
            
                # p.ln(2)
                p.cut()
                # p.clear()
                p.close()
                # print(f"Impresión ESCPOS exitosa en {printer_name} (papel: {paper_size}, fuente: {font_size})")
                return True, f"Impresión ESCPOS exitosa en {printer_name} (papel: {paper_size}, fuente: {font_size})"
            except Exception as e:
                logger.warning(f"Win32Raw falló: {e}")
        
        # Fallback: usar Dummy printer_name para debug
        p = printer.Dummy()
        process_html_for_escpos(p, html_content, paper_size, font_size, test_width, line_spacing)
        p.cut()
        output = p.output
        # print(f"Contenido que se enviaría a imprimir: {output[:200]}...")
        logger.info(f"Contenido que se enviaría a imprimir: {output[:200]}...")
        return True, f"Simulación de impresión en {printer_name} (papel: {paper_size}, fuente: {font_size}) (modo debug)"
        
    except Exception as e:
        logger.error(f"Error ESCPOS sistema: {e}")
        return False, f"Error en impresión ESCPOS: {str(e)}"
    

def get_char_width(paper_size, font_size='normal', content_type='default'):
    """
    Retorna el número de caracteres por línea según el papel, fuente y tipo de contenido
    
    Args:
        paper_size: '80mm' o '58mm'
        font_size: 'normal' o 'small'
        content_type: 'default', 'table', 'header', 'narrow'
    """
    # base_widths = {
    #     '80mm': {'normal': 48, 'small': 64},
    #     '58mm': {'normal': 32, 'small': 42}
    # }
    base_widths = {
        '80mm': {'normal': 48, 'small': 64},
        '58mm': {'normal': 32, 'small': 42}
    }
    
    # Obtener ancho base
    base_width = base_widths.get(paper_size, {}).get(font_size, 48)
    
    # Ajustar según el tipo de contenido
    if content_type == 'table':
        # Para tablas, usar ancho completo disponible
        if paper_size == '58mm':
            return base_width
        else:
            return int(min(base_width, base_width * 1))
    elif content_type == 'header':
        # Para encabezados, permitir un poco más de espacio
        return min(base_width + 4, base_width * 1.1)
    elif content_type == 'narrow':
        # Para contenido estrecho (QR, códigos de barras)
        return max(base_width - 8, base_width * 0.8)
    elif content_type == 'wide':
        # Para contenido que necesita más espacio        
        if paper_size == '58mm':
            return min(base_width, base_width * 0.78)
        else:
            return min(base_width, base_width * 0.7)
        
    if paper_size == '58mm':
        return int(base_width)
    else:
        return int(max(base_width, base_width * 1.59))
    

def set_line_spacing(p, spacing='normal'):
    """
    Configura el espaciado entre líneas usando comandos ESC/POS
    
    Args:
        p: objeto impresora escpos
        spacing: 'compact', 'normal', 'wide'
    """
    try:
        if spacing == 'compact':
            # Espaciado mínimo (ESC 3 n) - n = 10 (muy compacto)
            p._raw(bytes([0x1B, 0x33, 10]))
        elif spacing == 'wide':
            # Espaciado amplio (ESC 3 n) - n = 60
            p._raw(bytes([0x1B, 0x33, 60]))
        else:  # normal
            # Espaciado normal por defecto (ESC 3 n) - n = 50
            p._raw(bytes([0x1B, 0x33, 50]))
    except Exception as e:
        logger.warning(f"No se pudo configurar espaciado de líneas: {e}")

def print_char_width_test(p, paper_size):
    """Imprime línea de prueba numerada para verificar el ancho real"""
    try:
        p.text(f"Prueba de ancho para papel {paper_size}:\n")
        # Línea numerada hasta 65 caracteres
        test_line = ''.join([str(i % 10) for i in range(1, 66)])
        p.text(test_line + '\n')
        p.text("Marcar donde se corta la línea\n")
        p.text("=" * get_char_width(paper_size, 'normal') + '\n')
        p.ln(2)
    except Exception as e:
        print(f"Error en prueba de ancho: {e}")

def process_html_for_escpos(p, html_content, paper_size="80mm", font_size='normal', test_width=False, line_spacing='normal'):
    """Procesa e imprime HTML con formato según el tamaño de papel y fuente"""

    # Configurar espaciado de líneas al inicio
    set_line_spacing(p, line_spacing)

    # Obtener ancho de caracteres según papel y fuente
    char_width = get_char_width(paper_size, font_size)
    print(f"Ancho de caracteres para papel {paper_size} con fuente {font_size}: {char_width} caracteres por línea")
    # Imprimir prueba de ancho si se solicita
    if test_width:
        print_char_width_test(p, paper_size)
    
    soup = BeautifulSoup(html_content, "html.parser")
    
    # DEBUG: Mostrar longitud del HTML y primeros caracteres
    # print(f"HTML DEBUG: Longitud: {len(html_content)} caracteres")
    # print(f"HTML DEBUG: Primeros 200 chars: {html_content[:200]}")
    
    # DEBUG: Mostrar todos los elementos img encontrados
    img_elements = soup.find_all("img")
    # print(f"DEBUG: Elementos <img> encontrados: {len(img_elements)}")
    for i, img in enumerate(img_elements):
        src = img.get('src', 'NO SRC')
        # print(f"DEBUG: IMG {i+1}: src={src[:50]}...")

    def process_element(element):
        """Procesa un elemento HTML de manera recursiva evitando duplicaciones"""
        if not hasattr(element, 'name') or element.name is None:
            return
        
        element_name = element.name.lower()
        # print(f"DEBUG: Procesando elemento: {element_name}")

        # Encabezados h1 - h6
        if element_name in [f"h{i}" for i in range(1, 7)]:
            # print(f"Procesando encabezado: {element.name}")
            # Configurar tamaño según el tipo de encabezado (h1=más grande, h6=más pequeño)
            if element_name == "h1":
                width_size = 8
                height_size = 8
            elif element_name == "h2":                
                width_size = 7
                height_size = 7
            elif element_name == "h3":                
                width_size = 6
                height_size = 6
            elif element_name == "h4":                
                width_size = 5
                height_size = 5
            elif element_name == "h5":                
                width_size = 4
                height_size = 4
            elif element_name == "h6":                
                width_size = 3
                height_size = 3
            else:
                width_size = 1
                height_size = 1
            
            text = element.get_text(strip=True)
            # print(f"Encabezado texto: {text} - Tamaño: {width_size}x{height_size}")
            
            # Aplicar formato y tamaño
            try:
                # Aplicar formato con tamaño
                p.set(align="center", bold=False, underline=0)
                # Aplicar tamaño usando comando ESC ! directo
                if width_size > 1 or height_size > 1:
                    # size_param = ((width_size - 1) << 8) | (height_size - 1)
                    size_param = width_size
                    p._raw(bytes([0x1B, 0x21, size_param]))
                p.text(text)
                p.text("\n")  # Usar salto simple en lugar de p.ln()
                # Resetear tamaño y formato
                p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                p.set(align="left", bold=False)
            except:
                # Fallback: centrado manual
                padding = max(0, (char_width - len(text)) // 2)
                p.set(bold=True)
                if width_size > 1 or height_size > 1:
                    # size_param = ((width_size - 1) << 4) | (height_size - 1)
                    size_param = width_size
                    p._raw(bytes([0x1B, 0x21, size_param]))
                p.text(" " * padding + text + "\n")  # Reducir salto doble
                p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                p.set(bold=False)

        # Párrafos
        elif element_name == "p":
            p.set(align="left", bold=False, underline=0)
            p._raw(bytes([0x1B, 0x21, 1]))
            text = element.get_text(strip=True)
            
            # Obtener ancho dinámico para párrafos (puede ser diferente según contexto)
            paragraph_width = get_char_width(paper_size, font_size, 'default')
            
            # Ajustar texto al ancho dinámico
            if len(text) > paragraph_width:
                # Dividir texto largo en líneas con algoritmo mejorado
                words = text.split()
                lines = []
                current_line = ""
                for word in words:
                    test_line = current_line + (" " + word if current_line else word)
                    if len(test_line) <= paragraph_width:
                        current_line = test_line
                    else:
                        if current_line:
                            lines.append(current_line)
                            current_line = word
                        else:
                            # Palabra muy larga, dividir por caracteres
                            lines.append(word[:paragraph_width])
                            current_line = word[paragraph_width:] if len(word) > paragraph_width else ""
                if current_line:
                    lines.append(current_line)
                p.text("\n".join(lines) + "\n")
            else:
                p.text(text + "\n")

        # Centrados - procesar elementos hijos en lugar de imprimir directamente
        elif element_name == "center":
            # Procesar cada hijo del elemento center
            for child in element.children:
                if hasattr(child, 'name') and child.name:
                    # Es un elemento HTML
                    child_name = child.name.lower()
                    if child_name in [f"h{i}" for i in range(1, 7)]:
                        # Encabezado centrado
                        if child_name == "h1":
                            width_size, height_size = 8, 8
                        elif child_name == "h2":
                            width_size, height_size = 7, 7
                        elif child_name == "h3":
                            width_size, height_size = 6, 6
                        elif child_name == "h4":
                            width_size, height_size = 5, 5
                        elif child_name == "h5":
                            width_size, height_size = 4, 4
                        elif child_name == "h6":
                            width_size, height_size = 3, 3
                        else:
                            width_size, height_size = 1, 1
                        
                        text = child.get_text(strip=True)
                        # print(f"Encabezado centrado: {child_name} - {text} - Tamaño: {width_size}x{height_size}")
                        
                        try:
                            p.set(align="center", bold=True)
                            # Aplicar tamaño usando comando ESC ! directo
                            if width_size > 1 or height_size > 1:
                                # Calcular parámetro para ESC !
                                # Bits 0-3: altura (1-8), Bits 4-7: ancho (1-8)
                                # size_param = ((width_size - 1) << 4) | (height_size - 1)
                                size_param = width_size
                                p._raw(bytes([0x1B, 0x21, size_param]))
                            p.text(text)
                            p.text("\n")  # Usar salto simple
                            # Reset tamaño y formato
                            p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                            p.set(align="left", bold=False)
                        except:
                            padding = max(0, (char_width - len(text)) // 2)
                            p.set(bold=True)
                            if width_size > 1 or height_size > 1:
                                # size_param = ((width_size - 1) << 4) | (height_size - 1)
                                size_param = width_size
                                p._raw(bytes([0x1B, 0x21, size_param]))
                            p.text(" " * padding + text + "\n")
                            p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                            p.set(bold=False)
                    else:
                        # Otros elementos centrados
                        text = child.get_text(strip=True)
                        if text:
                            # print(f"Texto centrado: {text}")
                            try:
                                p.set(align="center")
                                p.text(text)
                                p.text("\n")  # Usar salto simple
                                p.set(align="left")
                            except:
                                padding = max(0, (char_width - len(text)) // 2)
                                p.text(" " * padding + text + "\n")
                elif hasattr(child, 'strip'):
                    # Es texto plano
                    text = child.strip()
                    if text:
                        # print(f"Texto plano centrado: {text}")
                        try:
                            p.set(align="center")
                            p.text(text)
                            p.text("\n")  # Usar salto simple
                            p.set(align="left")
                        except:
                            padding = max(0, (char_width - len(text)) // 2)
                            p.text(" " * padding + text + "\n")

        # Elementos con estilo text-align center
        elif element.get('style') and 'text-align:center' in element.get('style', ''):
            text = element.get_text(strip=True)
            if text:
                # print(f"Elemento con text-align center: {element_name} - {text}")
                # Aplicar tamaño si es encabezado
                if element_name in [f"h{i}" for i in range(1, 7)]:
                    if element_name == "h1":
                        width_size, height_size = 8, 8
                    elif element_name == "h2":
                        width_size, height_size = 7, 7
                    elif element_name == "h3":
                        width_size, height_size = 6, 6
                    elif element_name == "h4":
                        width_size, height_size = 5, 5
                    elif element_name == "h5":
                        width_size, height_size = 4, 4
                    elif element_name == "h6":
                        width_size, height_size = 3, 3
                    else:
                        width_size, height_size = 1, 1
                    
                    try:
                        p.set(align="center", bold=True)
                        if width_size > 1 or height_size > 1:
                            # size_param = ((width_size - 1) << 4) | (height_size - 1)
                            size_param = width_size
                            p._raw(bytes([0x1B, 0x21, size_param]))
                        p.text(text)
                        p.text("\n")  # Usar salto simple
                        p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                        p.set(align="left", bold=False)
                    except:
                        padding = max(0, (char_width - len(text)) // 2)
                        p.set(bold=True)
                        if width_size > 1 or height_size > 1:
                            # size_param = ((width_size - 1) << 4) | (height_size - 1)
                            size_param = width_size
                            p._raw(bytes([0x1B, 0x21, size_param]))
                        p.text(" " * padding + text + "\n")
                        p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                        p.set(bold=False)
                else:
                    try:
                        p.set(align="center")
                        p.text(text)
                        p.text("\n")  # Usar salto simple
                        p.set(align="left")
                    except:
                        padding = max(0, (char_width - len(text)) // 2)
                        p.text(" " * padding + text + "\n")

        # Negrita, cursiva, subrayado (solo si no están dentro de otros elementos procesados)
        elif element_name == "b":
            text = element.get_text(strip=True)
            if text:
                p.set(bold=True)
                p.text(text + "\n")
                p.set(bold=False)

        elif element_name == "i":
            text = element.get_text(strip=True)
            if text:
                p.set(italic=True)
                p.text(text + "\n")
                p.set(italic=False)

        elif element_name == "u":
            text = element.get_text(strip=True)
            if text:
                p.set(underline=1)
                p.text(text + "\n")
                p.set(underline=0)
        # Imágenes Base64
        elif element_name == "img":
            # print(f"DEBUG: Procesando elemento <img>")
            src = element.get("src", "")
            # print(f"DEBUG: src = {src[:100]}..." if len(src) > 100 else f"DEBUG: src = {src}")
            if "base64" in src:
                # print("DEBUG: Imagen base64 detectada, procesando...")
                try:
                    img_b64 = src.split(",")[1]
                    # print(f"DEBUG: Base64 extraído, longitud: {len(img_b64)}")
                    image_data = base64.b64decode(img_b64)
                    # print(f"DEBUG: Datos decodificados, tamaño: {len(image_data)} bytes")
                    image = Image.open(BytesIO(image_data))
                    # print(f"DEBUG: Imagen PIL cargada: {image.size[0]}x{image.size[1]}, modo: {image.mode}")
                except Exception as e:
                    # print(f"ERROR: Error procesando imagen base64: {e}")
                    return

                # Calcular ancho máximo en píxeles para el papel
                max_width_px = 576 if paper_size == "80mm" else 384  # 80mm ≈ 576px, 58mm ≈ 384px
                # print(f"DEBUG: Ancho máximo para papel {paper_size}: {max_width_px}px")
                
                # Obtener dimensiones desde atributos width/height o style
                width = None
                height = None
                # Atributos directos
                if element.has_attr("width"):
                    try:
                        width = int(element["width"])
                    except:
                        pass
                if element.has_attr("height"):
                    try:
                        height = int(element["height"])
                    except:
                        pass
                # Buscar en style
                if element.has_attr("style"):
                    style = element["style"]
                    import re
                    width_match = re.search(r"width\s*:\s*(\d+)px", style)
                    height_match = re.search(r"height\s*:\s*(\d+)px", style)
                    if width_match:
                        width = int(width_match.group(1))
                    if height_match:
                        height = int(height_match.group(1))

                # Redimensionar según el tamaño de papel
                orig_w, orig_h = image.size
                if width or height:
                    new_w = width if width else orig_w
                    new_h = height if height else orig_h
                else:
                    # Auto-ajustar al ancho del papel si no se especifica
                    if orig_w > max_width_px:
                        new_w = max_width_px
                        new_h = int((max_width_px * orig_h) / orig_w)
                    else:
                        new_w = orig_w
                        new_h = orig_h
                
                # Asegurar que no exceda el ancho máximo
                if new_w > max_width_px:
                    new_h = int((max_width_px * new_h) / new_w)
                    new_w = max_width_px
                
                # print(f"DEBUG: Redimensionando imagen a {new_w}x{new_h}")
                image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
                # print("DEBUG: Enviando imagen a impresora...")
                try:
                    p.image(image)
                    # print("DEBUG: Imagen enviada exitosamente")
                    p.text("\n")
                except Exception as e:
                    print(f"ERROR: Error enviando imagen a impresora: {e}")
            elif element.get("data-type") == "qr":
                data = element.get("data-value", "")
                if data:
                    qr_size = 15 if paper_size == "80mm" else 12  # QR más grande según papel
                    p.qr(data, size=qr_size)
                    p.text("\n")

        # Código de barras
        elif element_name == "barcode":
            code = element.get_text(strip=True)
            barcode_type = element.get("type", "CODE128")
            barcode_width = 2 if paper_size == "80mm" else 1  # Ajustar ancho según papel
            p.barcode(code, barcode_type, width=barcode_width, height=80, pos="BELOW", font="A")
            p.text("\n")

        # Tablas
        elif element_name == "table":
            # Usar ancho específico para tablas
            table_width = get_char_width(paper_size, font_size, 'table')
            # print(f"Procesando tabla con ancho {table_width} caracteres")
            print(f"{paper_size} - Ancho tabla: {table_width} caracteres")

            for tr in element.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if not cells:
                    continue
                
                # Verificar si esta fila tiene encabezados (th)
                has_headers = any(cell.name == "th" for cell in cells)
                
                # Verificar si alguna celda contiene un <hr>
                has_hr = any(cell.find("hr") is not None for cell in cells)
                
                if has_hr:
                    # Si la fila contiene un <hr>, crear una línea horizontal completa
                    # Usar el mismo ancho que la tabla para consistencia
                    hr_width = int(get_char_width(paper_size, font_size, 'wide'))
                    
                    # Buscar el primer HR para determinar el estilo
                    hr_element = None
                    for cell in cells:
                        hr_found = cell.find("hr")
                        if hr_found:
                            hr_element = hr_found
                            break
                    
                    # Determinar el carácter de línea
                    line_char = "-"  # Por defecto
                    if hr_element:
                        line_style = hr_element.get('style', '').lower() if hr_element.has_attr('style') else ''
                        line_class = hr_element.get('class', []) if hr_element.has_attr('class') else []
                        
                        if 'border-style:double' in line_style or 'double' in line_class:
                            line_char = "="
                        elif 'border-style:dotted' in line_style or 'dotted' in line_class:
                            line_char = "-"
                        elif 'border-style:dashed' in line_style or 'dashed' in line_class:
                            line_char = "-"
                        elif 'solid' in line_class or 'thick' in line_class:
                            line_char = "#"
                    
                    # Crear la línea horizontal
                    horizontal_line = line_char * hr_width
                    # print(f"HR en tabla: {hr_width} caracteres con '{line_char}'")
                    
                    # Imprimir la línea horizontal
                    try:
                        p.set(align="center", font=0)  # Usar alineación centrada para la línea horizontal
                        if paper_size == "58mm":
                            p.ln(-2)  # Espacio antes de la línea
                            p._raw(bytes([0x1B, 0x33, 5]))
                            p.text(horizontal_line)
                            p.ln()
                        else:                            
                            p.ln(-2)  # Espacio antes de la línea
                            p._raw(bytes([0x1B, 0x33, 5]))
                            p.block_text(horizontal_line)
                            p.ln()

                        set_line_spacing(p, line_spacing)
                        p.set(align="left", font=0)
                    except:
                        p.set(align="center", font=0, custom_size=True, width=1, height=1)  # Usar alineación centrada para la línea horizontal
                        p.text(horizontal_line)
                        p.ln(1)
                    
                    continue  # Saltar el procesamiento normal de la fila
                
                # Extraer texto de las celdas
                cols = [cell.get_text(strip=True).replace("\n", " ") for cell in cells]
                
                # Estrategia de ancho dinámico basada en contenido y número de columnas
                if len(cols) == 1:
                    # Una sola columna: usar todo el ancho disponible y aplicar estilos
                    cell = cells[0]
                    text = cols[0]
                    
                    # Analizar estilos de la celda única
                    import re
                    cell_align = 'left'  # Por defecto
                    is_numeric = bool(re.search(r'[\d$€£¥₹.,]+', text))
                    
                    # Analizar atributo style
                    if cell.has_attr('style'):
                        style = cell.get('style', '').lower()
                        
                        # Buscar text-align
                        if 'text-align:right' in style:
                            cell_align = 'right'
                        elif 'text-align:center' in style:
                            cell_align = 'center'
                        elif 'text-align:left' in style:
                            cell_align = 'left'
                    
                    # Si no hay estilo específico, inferir alineación del contenido
                    if cell_align == 'left' and is_numeric:
                        cell_align = 'right'  # Números a la derecha por defecto
                    
                    # Truncar texto si es muy largo
                    truncated_text = text[:table_width] if len(text) > table_width else text
                    
                    # Aplicar alineación según el estilo
                    if cell_align == 'right':
                        line = truncated_text.rjust(table_width)
                    elif cell_align == 'center':
                        line = truncated_text.center(table_width)
                    else:  # left o por defecto
                        line = truncated_text.ljust(table_width)
                else:
                    # Múltiples columnas: análisis dinámico de estilos CSS y contenido
                    cell_styles = []
                    total_fixed_width = 0
                    flexible_columns = []
                    content_lengths = [len(col) for col in cols]  # Longitudes del contenido
                    
                    # Asegurar importación de re
                    import re
                    
                    # Analizar estilos de cada celda y contenido
                    for i, cell in enumerate(cells):
                        style_info = {
                            'width_percent': None,
                            'width_fixed': None,
                            'align': 'left',  # Por defecto
                            'index': i,
                            'content_length': content_lengths[i],
                            'is_numeric': bool(re.search(r'[\d$€£¥₹.,]+', cols[i]))
                        }
                        
                        # Analizar atributo style
                        if cell.has_attr('style'):
                            style = cell.get('style', '').lower()
                            
                            # Buscar text-align
                            if 'text-align:right' in style:
                                style_info['align'] = 'right'
                            elif 'text-align:center' in style:
                                style_info['align'] = 'center'
                            elif 'text-align:left' in style:
                                style_info['align'] = 'left'
                            else:
                                style_info['align'] = 'left'  # Por defecto
                            
                            # Buscar width en porcentaje
                            import re
                            width_percent_match = re.search(r'width\s*:\s*(\d+(?:\.\d+)?)%', style)
                            if width_percent_match:
                                style_info['width_percent'] = float(width_percent_match.group(1))
                            
                            # Buscar width en píxeles, caracteres o unidades
                            width_px_match = re.search(r'width\s*:\s*(\d+)(?:px|ch|em|%)?', style)
                            if width_px_match:
                                # Conversión inteligente: px/8, ch=1, em*14
                                value = int(width_px_match.group(1))
                                if 'ch' in style:
                                    style_info['width_fixed'] = value
                                elif 'em' in style:
                                    style_info['width_fixed'] = max(1, int(value * 1.4))
                                elif '%' in style:
                                    style_info['width_percent'] = float(value)
                                else:  # px por defecto
                                    style_info['width_fixed'] = max(1, value // 8)
                        
                        # Si no hay estilo específico, inferir alineación del contenido
                        # if style_info['align'] == 'left' and style_info['is_numeric']:
                        #     style_info['align'] = 'right'  # Números a la derecha por defecto
                        
                        cell_styles.append(style_info)
                        
                        # Acumular anchos fijos
                        if style_info['width_fixed']:
                            total_fixed_width += style_info['width_fixed']
                        elif not style_info['width_percent']:
                            flexible_columns.append(i)
                    
                    # Calcular anchos de columnas con distribución inteligente
                    col_widths = []
                    
                    # Calcular anchos basados en porcentajes
                    for style in cell_styles:
                        if style['width_percent']:
                            width = max(1, int(table_width * style['width_percent'] / 100))
                            col_widths.append(width)
                        elif style['width_fixed']:
                            col_widths.append(style['width_fixed'])
                        else:
                            col_widths.append(0)  # Se calculará después
                    
                    # Distribuir espacio restante entre columnas flexibles
                    remaining_for_flexible = table_width - sum(col_widths)
                    if flexible_columns and remaining_for_flexible > 0:
                        # Distribución basada en contenido para columnas flexibles
                        total_content_length = sum(cell_styles[i]['content_length'] for i in flexible_columns)
                        
                        if total_content_length > 0:
                            # Distribución proporcional al contenido
                            for col_idx in flexible_columns:
                                content_ratio = cell_styles[col_idx]['content_length'] / total_content_length
                                col_widths[col_idx] = max(1, int(remaining_for_flexible * content_ratio))
                        else:
                            # Distribución uniforme si no hay contenido
                            flex_width = max(1, remaining_for_flexible // len(flexible_columns))
                            extra_width = remaining_for_flexible % len(flexible_columns)
                            
                            for i, col_idx in enumerate(flexible_columns):
                                col_widths[col_idx] = flex_width + (1 if i < extra_width else 0)
                    
                    # Asegurar que la suma no exceda el ancho total
                    total_width = sum(col_widths)
                    if total_width > table_width:
                        # Reducir proporcionalmente
                        factor = table_width / total_width
                        col_widths = [max(1, int(w * factor)) for w in col_widths]
                        # Ajustar diferencia restante
                        diff = table_width - sum(col_widths)
                        for i in range(min(abs(diff), len(col_widths))):
                            col_widths[i] += 1 if diff > 0 else -1
                    
                    # Formatear columnas con estilos aplicados
                    formatted_cols = []
                    for i, (col, width, style) in enumerate(zip(cols, col_widths, cell_styles)):
                        # Truncar texto si es muy largo
                        truncated_text = col[:width] if len(col) > width else col
                        
                        # Aplicar alineación según el estilo
                        if style['align'] == 'right':
                            formatted_col = truncated_text.rjust(width)
                        elif style['align'] == 'center':
                            formatted_col = truncated_text.center(width)
                        else:  # left o por defecto
                            formatted_col = truncated_text.ljust(width)
                        
                        formatted_cols.append(formatted_col)
                        
                        # Debug: mostrar información de estilo
                        # if style['width_percent'] or style['width_fixed'] or style['align'] != 'left':
                            # print(f"  Columna {i}: '{col[:20]}...' -> ancho={width}, align={style['align']}, width_style={style['width_percent'] or style['width_fixed']}")
                    
                    line = "".join(formatted_cols)
                
                # Asegurar que la línea use exactamente el ancho de la tabla
                if len(line) > table_width:
                    line = line[:table_width]
                elif len(line) < table_width:
                    line = line.ljust(table_width)
                
                # Aplicar formato según si tiene encabezados (th)
                if has_headers:
                    # print(f"Fila de tabla con encabezados (th): {line}")
                    p.set(bold=True, align="center")  # Negrita para encabezados
                else:
                    # print(f"Fila de tabla normal (td): {line}")
                    p.set(bold=False)  # Sin negrita para celdas normales

                # Imprimir la línea de la tabla
                if paper_size == "58mm":
                    p._raw(bytes([0x1B, 0x21, 5]))  # Tamaño ligeramente más grande para tablas              
                    p.block_text(line)
                    p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                    p.set(bold=False)  # Reset negrita
                    p.ln(1)
                else:
                    p._raw(bytes([0x1B, 0x21, 5]))  # Tamaño ligeramente más grande para tablas              
                    p.text(line)
                    p._raw(bytes([0x1B, 0x21, 0x00]))  # Reset tamaño
                    p.set(bold=False)  # Reset negrita
                    p.ln(1)

        # Línea horizontal <hr>
        elif element_name == "hr":
            # Obtener ancho completo del papel para la línea horizontal
            hr_width = int(get_char_width(paper_size, font_size, 'wide'))
            
            # Detectar el tipo de línea desde atributos o estilos
            line_char = "-"  # Por defecto
            line_style = element.get('style', '').lower() if element.has_attr('style') else ''
            line_class = element.get('class', []) if element.has_attr('class') else []
            
            # Determinar el carácter de línea según estilos o clases
            if 'border-style:double' in line_style or 'double' in line_class:
                line_char = "="
            elif 'border-style:dotted' in line_style or 'dotted' in line_class:
                line_char = "-"
            elif 'border-style:dashed' in line_style or 'dashed' in line_class:
                line_char = "-"
            elif 'solid' in line_class or 'thick' in line_class:
                line_char = "#"  # Usar # para líneas sólidas (compatible ASCII)
            
            # Crear la línea horizontal
            horizontal_line = line_char * hr_width
            
            # print(f"Línea horizontal: {hr_width} caracteres con '{line_char}'")
            
            # Imprimir la línea
            try:
                p.set(align="center", font=0)  # Usar alineación centrada para la línea horizontal
                p.ln(-2)  # Espacio antes de la línea
                p._raw(bytes([0x1B, 0x33, 5]))
                p.text(horizontal_line)
                p.ln()
                set_line_spacing(p, line_spacing)
            except:
                # Fallback simple
                p.set(align="center", font=0, custom_size=True, width=1, height=1)  # Usar alineación centrada para la línea horizontal
                p.text(horizontal_line + "\n")

        # Código QR directo (etiqueta personalizada)
        elif element_name == "qr":
            data = element.get_text(strip=True)
            qr_size = 8 if paper_size == "80mm" else 12  # QR más grande según papel
            p.qr(data, size=qr_size)
            p.text("\n")

        # Elementos contenedores: procesar hijos recursivamente
        elif element_name in ["div", "section", "article", "main", "header", "footer"]:
            # print(f"DEBUG: Procesando contenedor {element_name}, hijos: {[child.name for child in element.children if hasattr(child, 'name') and child.name]}")
            for child in element.children:
                if hasattr(child, 'name') and child.name:
                    process_element(child)

        # Para cualquier otro elemento, procesar recursivamente sus hijos
        else:
            for child in element.children:
                if hasattr(child, 'name') and child.name:
                    process_element(child)

    # Iniciar el procesamiento desde los elementos de nivel superior
    body = soup.find('body') or soup
    for element in body.children:
        if hasattr(element, 'name') and element.name:
            process_element(element)


def get_usb_printers():
    """Obtener impresoras USB disponibles"""
    printers = []
    try:
        # Buscar dispositivos USB que podrían ser impresoras
        devices = usb.core.find(find_all=True)
        for device in devices:
            try:
                # Verificar si es una impresora (clase 7)
                if device.bDeviceClass == 7 or any(
                    interface.bInterfaceClass == 7 
                    for config in device 
                    for interface in config
                ):
                    printer_name = f"USB_Printer_{device.idVendor:04x}:{device.idProduct:04x}"
                    printers.append({
                        "name": printer_name,
                        "connection_type": "usb",
                        "status": "available",
                        "description": f"Vendor ID: {device.idVendor:04x}, Product ID: {device.idProduct:04x}"
                    })
            except Exception as e:
                logger.warning(f"Error al procesar dispositivo USB: {e}")
                continue
    except Exception as e:
        logger.error(f"Error al buscar impresoras USB: {e}")
    
    return printers

def get_network_printers():
    """Obtener impresoras de red disponibles"""
    printers = []
    
    # Agregar impresoras de red desde la configuración
    if "network" in config["printers"]:
        network_config = config["printers"]["network"]
        ip = network_config.get("ip")
        port = network_config.get("port", 9100)
        
        if ip:
            # Verificar si la impresora está disponible
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex((ip, port))
                sock.close()
                
                status = "available" if result == 0 else "offline"
                printers.append({
                    "name": f"Network_Printer_{ip}",
                    "connection_type": "network",
                    "status": status,
                    "description": f"IP: {ip}, Puerto: {port}",
                    "puerto": str(port)
                })
            except Exception as e:
                logger.error(f"Error al verificar impresora de red: {e}")
                printers.append({
                    "name": f"Network_Printer_{ip}",
                    "connection_type": "network",
                    "status": "error",
                    "description": f"IP: {ip}, Puerto: {port} - Error: {str(e)}",
                    "puerto": str(port)
                })
    
    return printers

def get_system_printers():
    """Obtener impresoras del sistema"""
    printers = []
    
    try:
        if platform.system() == "Windows":
            # Método 1: Usar win32print si está disponible
            if WIN32_AVAILABLE:
                try:
                    printer_list = win32print.EnumPrinters(2)
                    for printer_info in printer_list:
                        # printer_info es una tupla: (Flags, Description, Name, Comment)
                        if len(printer_info) >= 3:
                            printer_name = printer_info[2]  # Nombre de la impresora
                            description = printer_info[1] if len(printer_info) > 1 else "N/A"
                            comment = printer_info[4] if len(printer_info) > 4 else "N/A"
                            
                            printers.append({
                                "name": printer_name,
                                "connection_type": "system",
                                "status": "available",
                                "description": f"Descripción: {description}, Comentario: {comment}"
                            })
                except Exception as e:
                    logger.warning(f"Error con win32print: {e}")
            
            # Método 2: Fallback con PowerShell si win32print falla
            if not printers:
                try:
                    result = subprocess.run([
                        "powershell", "-Command", 
                        "Get-Printer | Select-Object Name, DriverName, PortName, PrinterStatus | ConvertTo-Json"
                    ], capture_output=True, text=True, shell=True)
                    
                    if result.returncode == 0:
                        try:
                            printer_data = json.loads(result.stdout)
                            if isinstance(printer_data, dict):
                                printer_data = [printer_data]
                            
                            for printer in printer_data:
                                status = "available"
                                if printer.get("PrinterStatus"):
                                    status = "offline" if "offline" in str(printer["PrinterStatus"]).lower() else "available"
                                
                                printers.append({
                                    "name": printer["Name"],
                                    "connection_type": "system",
                                    "status": status,
                                    "description": f"Driver: {printer.get('DriverName', 'N/A')}, Puerto: {printer.get('PortName', 'N/A')}",
                                    "puerto": printer.get('PortName', 'N/A')
                                })
                        except json.JSONDecodeError as e:
                            logger.error(f"Error al parsear salida de PowerShell: {e}")
                except Exception as e:
                    logger.error(f"Error con PowerShell: {e}")
        
        elif platform.system() == "Linux":
            # Usar lpstat para Linux
            result = subprocess.run(["lpstat", "-p"], capture_output=True, text=True)
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines:
                    if line.startswith('printer '):
                        parts = line.split()
                        if len(parts) >= 2:
                            printer_name = parts[1]
                            status = "available" if "idle" in line else "busy"
                            printers.append({
                                "name": printer_name,
                                "connection_type": "system",
                                "status": status,
                                "description": "Impresora del sistema Linux"
                            })
    
    except Exception as e:
        logger.error(f"Error al obtener impresoras del sistema: {e}")
    
    return printers

def get_status_printer(printer_name):
    """Obtener estado de una impresora específica"""
    from escpos import printer
    if WIN32_AVAILABLE:
        try:
            # print(f"Verificando estado de la impresora: {printer_name}")
            p = printer.Win32Raw(printer_name)
            # status = p.is_online()            
            # print(f"Estado de la impresora {printer_name}: {'En línea' if status else 'Fuera de línea'}")
            # return status
            p.buzzer(times=2, duration=4)
            return True
        except Exception as e:
            return False
    return False

def cash_drawer(printer_name):
    """Send pulse to kick the cash drawer."""
    from escpos import printer
    if WIN32_AVAILABLE:
        try:
            # print(f"Verificando estado de la impresora: {printer_name}")
            p = printer.Win32Raw(printer_name)
            p.cashdraw(config("cashdraw_pin"))
            return True
        except Exception as e:
            return False
    return False


def buzzer(printer_name):
    """Send pulse to kick the cash drawer."""
    from escpos import printer
    if WIN32_AVAILABLE:
        try:
            # print(f"Verificando estado de la impresora: {printer_name}")
            p = printer.Win32Raw(printer_name)
            p.buzzer(times=2, duration=4)
            return True
        except Exception as e:
            return False
    return False

# Endpoints de la API
@app.get("/")
async def root():
    """Página principal con interfaz web"""
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>PrintPOS API</title>
        <script src="https://unpkg.com/qrcode@1.5.3/build/qrcode.min.js"></script>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            .header {
                text-align: center;
                margin-bottom: 30px;
                color: #333;
            }
            .section {
                margin: 20px 0;
                padding: 20px;
                border: 1px solid #ddd;
                border-radius: 5px;
                background: #fafafa;
            }
            .form-group {
                margin: 15px 0;
            }
            label {
                display: block;
                margin-bottom: 5px;
                font-weight: bold;
                color: #555;
            }
            input, select, textarea, button {
                width: 100%;
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }
            button {
                background-color: #007bff;
                color: white;
                border: none;
                cursor: pointer;
                font-size: 16px;
                margin: 5px 0;
            }
            button:hover {
                background-color: #0056b3;
            }
            .btn-secondary {
                background-color: #6c757d;
            }
            .btn-secondary:hover {
                background-color: #545b62;
            }
            .response {
                margin: 10px 0;
                padding: 10px;
                border-radius: 4px;
                min-height: 100px;
                overflow-x: auto;
            }
            .success {
                background-color: #d4edda;
                border: 1px solid #c3e6cb;
                color: #155724;
            }
            .error {
                background-color: #f8d7da;
                border: 1px solid #f5c6cb;
                color: #721c24;
            }
            .info {
                background-color: #d1ecf1;
                border: 1px solid #bee5eb;
                color: #0c5460;
            }
            pre {
                white-space: pre-wrap;
                word-wrap: break-word;
            }
            .grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }
            @media (max-width: 768px) {
                .grid {
                    grid-template-columns: 1fr;
                }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>PrintPOS API</h1>
                <p>API para impresión de tickets POS en impresoras térmicas</p>
            </div>

            <div class="grid">
                <!-- Sección de información -->
                <div class="section">
                    <h3>📋 Información del Sistema</h3>
                    <button onclick="getVersion()">Obtener Versión</button>
                    <div id="versionResponse" class="response info"></div>

                    <button onclick="getStatus()">Ver Estado</button>
                    <div id="statusResponse" class="response info"></div>

                    <button onclick="getCashDrawerStatus()">Abrir Cajón de Dinero</button>
                    <div id="cashDrawerResponse" class="response info"></div>

                    <button onclick="getBuzzer()">Activar Buzzer</button>
                    <div id="buzzerResponse" class="response info"></div>

                    <button onclick="listPrinters()">Listar Impresoras</button>
                    <div id="printersResponse" class="response info"></div>
                </div>

                <!-- Sección de impresión -->
                <div class="section">
                    <div class="section">
                        <h3>📝 Ejemplos de HTML</h3>
                        <div id="qrStatus" style="font-size: 12px; color: #666; margin-bottom: 10px;">⏳ Verificando librería QR...</div>
                        <button onclick="loadExample('ticket')">Ticket de Venta</button>
                        <button onclick="loadExample('receipt')">Recibo</button>
                        <button onclick="loadExample('invoice')">Factura Simple</button>                
                    </div>
                    <h3>Enviar a Imprimir</h3>
                    <div class="form-group">
                        <label for="printerSelect">Seleccionar Impresora:</label>
                        <select id="printerSelect">
                            <option value="">Cargar impresoras primero...</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label for="paperSize">Tamaño de Papel:</label>
                        <select id="paperSize">
                            <option value="80mm">80mm (48 chars normal / 64 chars pequeña)</option>
                            <option value="58mm">58mm (32 chars normal / 42 chars pequeña)</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label for="fontSize">Tamaño de Fuente:</label>
                        <select id="fontSize">
                            <option value="normal">Normal (más legible)</option>
                            <option value="small">Pequeña (más contenido)</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label for="testWidth">
                            <input type="checkbox" id="testWidth"> 
                            Imprimir línea de prueba de ancho (para calibración)
                        </label>
                    </div>
                    
                    <div class="form-group">
                        <label for="htmlContent">Contenido HTML:</label>
                        <textarea id="htmlContent" rows="10" placeholder="Ingrese el código HTML a imprimir...">
<div>
    <h2>TICKET DE PRUEBA</h2>
    <p>================================</p>
    <p><strong>Fecha:</strong> 2025-01-01</p>
    <p><strong>Ticket #:</strong> 001</p>
    <p>================================</p>
    <table style="width: 100%; font-family: monospace;">
        <tr>
            <td>Producto A</td>
            <td style="text-align: right;">$10.00</td>
        </tr>
        <tr>
            <td>Producto B</td>
            <td style="text-align: right;">$10.00</td>
        </tr>
        <tr>
            <td>Producto C</td>
            <td style="text-align: right;">$10.00</td>
        </tr>
        <tr>
            <td>Producto D</td>
            <td style="text-align: right;">$10.50</td>
        </tr>
        <tr style="border-top: 1px solid black;">
            <td><strong>TOTAL:</strong></td>
            <td style="text-align: right;"><strong>$40.50</strong></td>
        </tr>
    </table>
    <p>================================</p>
    <center>
    <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAnsAAAJ7AQMAAACh+sXUAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAGUExURQAAAP///6XZn90AAAAJcEhZcwAADsMAAA7DAcdvqGQAAANcSURBVHja7d1NbtswEIbhMbLIUj1AAR3FR0uP5qMQ6AHqpRdG2YrSiByK+auJYFS9s2Is8zG/oA1BikqkUS/xt8hznOrH9PXc1KvnqfmUXrps+469QXE/QiI/Dor7ERL5cVDcj5DIj4PifoQTOF/UChP4t26LXdRdZMjK3DR95z59wdE9SGQiE5nIRN5Z5KuszZCVYGZUVaKZRtPHpDqVI3QMEpnIRCYykYncAlOtoE6jRfMrQHE/QiITmchEJvL+I08Xpy3TUwKCrHXOM+qnIv8jOLoHiUxkIhOZyDuLXNS7i0dVPrsH6wU8YGRAQEDAHYObUiWYZr3AG/ItvqIAAQEBAd8oV+AhI8dWrcq6JmxOo40K7sFIZH8jJPLjYAQEBPwQWLyv3uEUA+a9TDF2UXnxCOgJFCITmchEJnJ1X694DC9No7IoRfNuPlmruK83VW8wuAeJTGQiExlwT6Bsp9H24nE99pJ6zoMABAQE/BAYiExkF5FjWXeZD25u6o3F4/xqPlLTGYzuQSITmchEJvJ+IpvZsbh4M/ZYLi3ja+vINELvIJGJTGQiE/nAkYvZsZ5Gx+U9t3x1c3dQ++YHADuD4n6ERCYyICDgnsCYtz+j2TKNW7tWivEAAgICAgL2BrX0Ht24/P6UW27a0gXeNf9WlWZ1Akf3IJGJTGQi7zTyIUGdB/UMZ3GERcztOl3rxeWT39vhBAQEBAQEdAemGsxrjdMsYu6TaJ9L3hUFBAQEBATcC2i7mC3TU37nk3mw75o7lgtOQEBAQEBA5+CQu6SyT6k/14vHehDlCRhAQEBAQED/oG5/BrNP+rLYqWusH3EwVU+jgICAgICA7sDYqnXxGLOtzYuZUbMNCAgICAjoHmxU4+/rpUMzdp/U3ihc91YBAQEBAQE9g/ZUZ714XJv1itFOo+vNRUBAQEBAQOegzo5PZu7UuuWrcdlb3RQgICAgIODXgdX7pprnN300Xdd6WoCAgICAgPsEN1XscJ6rS/fNGU5AQEBAQEDPoO2q06g+uTCXHmG5VAdb9OPyaZaDgdIb/OZ+hN3BgX+H/sCTHO6/ngg/vv7/yN+lUbpiDMvX8+IxNQfTvJR9luoNBvdgdA/+6g36/x6G44Hdv4c/iewvMj8PHwZj+ANgz1jxptcbSwAAAABJRU5ErkJggg==" 
    alt="Código QR" 
    style="width:50px;height:50px;display:block;margin:10px auto;">
    </center>
    <p>¡ Gracias por su compra!</p>
    <p>Vuelva pronto áéíóúñ@*/&%!"#$%/()=?¡</p>
</div>
                        </textarea>
                    </div>
                    
                    <button onclick="sendToPrint()">Enviar a Imprimir</button>
                    <button onclick="previewHTML()" class="btn-secondary">👁️ Vista Previa HTML</button>
                    <div id="printResponse" class="response info"></div>
                    
                    <!-- Área de vista previa -->
                    <div id="previewArea" style="display: none; margin-top: 20px; padding: 15px; border: 2px solid #007bff; border-radius: 5px; background: #f8f9fa;">
                        <h4>👁️ Vista Previa del HTML</h4>
                        <div id="previewContent" style="border: 1px solid #ddd; padding: 10px; background: white; max-height: 400px; overflow-y: auto;"></div>
                        <button onclick="hidePreview()" style="margin-top: 10px;">Cerrar Vista Previa</button>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let currentPrinters = [];

            async function makeRequest(url, options = {}) {
                try {
                    const response = await fetch(url, {
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        ...options
                    });
                    const data = await response.json();
                    return { success: response.ok, data };
                } catch (error) {
                    return { success: false, error: error.message };
                }
            }

            function displayResponse(elementId, response, isSuccess = true) {
                const element = document.getElementById(elementId);
                if (!element) {
                    console.warn('Elemento no encontrado:', elementId);
                    return;
                }
                
                element.className = `response ${isSuccess ? 'success' : 'error'}`;
                element.innerHTML = `<pre>${JSON.stringify(response, null, 2)}</pre>`;
            }

            async function getVersion() {
                const result = await makeRequest('/version');
                displayResponse('versionResponse', result.data || result.error, result.success);
            }

            async function getStatus() {
                const printer = document.getElementById('printerSelect').value;                
                if (!printer) {
                    alert('Por favor seleccione una impresora');
                    return;
                }

                const printData = {
                    printer: printer, 
                    size: '80mm',
                    html: '<p>Estado de la impresora</p>',
                    font_size: 'normal',
                    test_width: false
                };

                const result = await makeRequest('/status', {
                    method: 'POST',
                    body: JSON.stringify(printData)
                });
                displayResponse('statusResponse', result.data || result.error, result.success);
            }

            async function getCashDrawerStatus() {
                const printer = document.getElementById('printerSelect').value;

                if (!printer) {
                    alert('Por favor seleccione una impresora');
                    return;
                }

                const printData = {
                    printer: printer, 
                    size: '80mm',
                    html: '<p>Abriendo cajón de dinero</p>',
                    font_size: 'normal',
                    test_width: false
                };

                const result = await makeRequest('/cash_drawer', {
                    method: 'POST',
                    body: JSON.stringify(printData)
                });
                displayResponse('cashDrawerResponse', result.data || result.error, result.success);
            }

            async function getBuzzer() {
                const printer = document.getElementById('printerSelect').value;

                if (!printer) {
                    alert('Por favor seleccione una impresora');
                    return;
                }

                const printData = {
                    printer: printer, 
                    size: '80mm',
                    html: '<p>buzzer</p>',
                    font_size: 'normal',
                    test_width: false
                };

                const result = await makeRequest('/buzzer', {
                    method: 'POST',
                    body: JSON.stringify(printData)
                });
                displayResponse('buzzerResponse', result.data || result.error, result.success);
            }            

            async function listPrinters() {
                const result = await makeRequest('/list_prints');
                if (result.success) {
                    currentPrinters = result.data;
                    updatePrinterSelect();
                    displayResponse('printersResponse', result.data, true);
                } else {
                    displayResponse('printersResponse', result.error, false);
                }
            }

            function updatePrinterSelect() {
                const select = document.getElementById('printerSelect');
                select.innerHTML = '<option value="">Seleccione una impresora...</option>';
                
                currentPrinters.forEach(printer => {
                    const option = document.createElement('option');
                    option.value = printer.name;
                    option.textContent = `${printer.name} (${printer.connection_type}) - ${printer.status}`;
                    option.disabled = printer.status !== 'available';
                    select.appendChild(option);
                });
            }

            async function sendToPrint() {
                const printer = document.getElementById('printerSelect').value;
                const size = document.getElementById('paperSize').value;
                const html = document.getElementById('htmlContent').value;
                const fontSize = document.getElementById('fontSize').value;
                const testWidth = document.getElementById('testWidth').checked;

                if (!printer) {
                    alert('Por favor seleccione una impresora');
                    return;
                }

                if (!html.trim() && !testWidth) {
                    alert('Por favor ingrese contenido HTML o active la prueba de ancho');
                    return;
                }

                const printData = {
                    printer: printer,
                    size: size,
                    html: html,
                    font_size: fontSize,
                    test_width: testWidth
                };

                const result = await makeRequest('/send_printer', {
                    method: 'POST',
                    body: JSON.stringify(printData)
                });

                displayResponse('printResponse', result.data || result.error, result.success);
            }

            function loadExample(type) {
                console.log('Cargando ejemplo:', type);
                
                // Ejemplos base sin QR
                const baseExamples = {
                    ticket: `<div style="text-align: center; font-family: monospace; font-size: 12px;">
    <h2>MINIMARKET LA ESQUINA</h2>
    <p>Calle Principal #123</p>
    <p>Tel: (555) 123-4567</p>
    <p>================================</p>
    <p><strong>TICKET DE VENTA</strong></p>
    <p>Fecha: ${new Date().toLocaleString()}</p>
    <p>Cajero: Ana García</p>
    <p>Ticket: #${Math.floor(Math.random() * 1000).toString().padStart(4, '0')}</p>
    <p>================================</p>
    <table style="width: 100%; font-size: 11px;">
        <tr><td>2x Coca Cola 500ml</td><td style="text-align: right;">$4.00</td></tr>
        <tr><td>1x Pan Integral</td><td style="text-align: right;">$2.50</td></tr>
        <tr><td>3x Huevos (docena)</td><td style="text-align: right;">$9.00</td></tr>
        <tr><td>1x Leche 1L</td><td style="text-align: right;">$3.25</td></tr>
    </table>
    <p>================================</p>
    <p><strong>TOTAL: $21.75</strong></p>
    <p>================================</p>
    <p>¡Gracias por su compra!</p>
</div>`,
                    receipt: `<div style="text-align: center; font-family: Arial, sans-serif; font-size: 14px;">
    <h2>💳 COMPROBANTE DE PAGO</h2>
    <p>================================</p>
    <p><strong>SERVICIO:</strong> Pago de Servicios</p>
    <p><strong>FECHA:</strong> ${new Date().toLocaleString()}</p>
    <p><strong>REFERENCIA:</strong> ${Math.random().toString(36).substr(2, 9).toUpperCase()}</p>
    <p>================================</p>
    <p>Concepto: Electricidad</p>
    <p>Importe: <strong>$85.50</strong></p>
    <p>================================</p>
    <p><strong>ESTADO: PAGADO ✅</strong></p>
    <p>================================</p>
</div>`,
                    invoice: `<div style="font-family: Arial, sans-serif; font-size: 12px;">
    <h2 style="text-align: center;">📄 FACTURA SIMPLIFICADA</h2>
    <p style="text-align: center;">COMERCIAL EJEMPLO S.A. DE C.V.</p>
    <p style="text-align: center;">RFC: CEJ123456789</p>
    <hr>
    <p><strong>Cliente:</strong> Público en General</p>
    <p><strong>Fecha:</strong> ${new Date().toLocaleDateString()}</p>
    <p><strong>Folio:</strong> ${Math.floor(Math.random() * 10000)}</p>
    <hr>
    <p>Servicio técnico: $250.00</p>
    <p>Refacciones: $90.00</p>
    <hr>
    <p style="text-align: right;"><strong>Total: $394.40</strong></p>
    <hr>
</div>`
                };

                // Cargar HTML base
                document.getElementById('htmlContent').value = baseExamples[type];
                console.log('Ejemplo base cargado. Ahora generando QR...');
                
                // Generar QR después de cargar
                setTimeout(() => addQRToExample(type), 100);
            }

            function generateQRCodes(type) {
                // Verificar si QRCode está disponible
                if (typeof QRCode === 'undefined') {
                    console.warn('QRCode library not loaded, using server-side generation');
                    generateQRCodesServerSide(type);
                    return;
                }

                // Limpiar QRs anteriores
                const qrContainers = ['qr-ticket', 'qr-receipt', 'qr-invoice'];
                qrContainers.forEach(id => {
                    const element = document.getElementById(id);
                    if (element) {
                        element.innerHTML = '';
                    }
                });

                // Generar QR específico según el tipo de ejemplo
                if (type === 'ticket') {
                    const ticketData = `TICKET-${Math.floor(Math.random() * 10000)}\nFecha: ${new Date().toLocaleDateString()}\nTotal: $21.75\nVerificar en: minimarket-laesquina.com`;
                    generateQRForElement('qr-ticket', ticketData, 300);
                } else if (type === 'receipt') {
                    const receiptData = `COMPROBANTE-${Math.random().toString(36).substr(2, 9).toUpperCase()}\nServicio: Electricidad\nImporte: $85.50\nVerificar en: pagos-servicios.com`;
                    generateQRForElement('qr-receipt', receiptData, 300);
                } else if (type === 'invoice') {
                    const invoiceData = `CFDI-UUID: 12345678-1234-1234-1234-123456789012\nRFC: CEJ123456789\nTotal: $394.40\nVerificar en: verificacfdi.facturaelectronica.sat.gob.mx`;
                    generateQRForElement('qr-invoice', invoiceData, 300);
                }
            }

            async function generateQRCodesServerSide(type) {
                // Generar QR usando el servidor
                let qrData = '';
                let elementId = '';
                let size = 80;

                if (type === 'ticket') {
                    qrData = `TICKET-${Math.floor(Math.random() * 10000)}\nFecha: ${new Date().toLocaleDateString()}\nTotal: $21.75\nVerificar en: minimarket-laesquina.com`;
                    elementId = 'qr-ticket';
                } else if (type === 'receipt') {
                    qrData = `COMPROBANTE-${Math.random().toString(36).substr(2, 9).toUpperCase()}\nServicio: Electricidad\nImporte: $85.50\nVerificar en: pagos-servicios.com`;
                    elementId = 'qr-receipt';
                } else if (type === 'invoice') {
                    qrData = `CFDI-UUID: 12345678-1234-1234-1234-123456789012\nRFC: CEJ123456789\nTotal: $394.40\nVerificar en: verificacfdi.facturaelectronica.sat.gob.mx`;
                    elementId = 'qr-invoice';
                    size = 100;
                }
            }

            function generateQRForElement(elementId, data, size) {
                const element = document.getElementById(elementId);
                if (!element) {
                    console.warn('Element not found:', elementId);
                    return;
                }

                if (typeof QRCode !== 'undefined') {
                    // Limpiar contenido anterior
                    element.innerHTML = '';
                    
                    // Crear un canvas para el QR
                    const canvas = document.createElement('canvas');
                    element.appendChild(canvas);
                    
                    QRCode.toCanvas(canvas, data, {
                        width: size,
                        height: size,
                        margin: 1,
                        color: {
                            dark: '#000000',
                            light: '#ffffff'
                        }
                    }, function (error) {
                        if (error) {
                            console.error('Error generando QR:', error);
                            element.innerHTML = `<div style="background: #f0f0f0; width: ${size}px; height: ${size}px; display: flex; align-items: center; justify-content: center; font-size: 10px; border: 1px solid #ccc;">QR Error</div>`;
                        }
                    });
                } else {
                    console.warn('QRCode library not available, using fallback');
                    element.innerHTML = `<div style="background: #e0e0e0; width: ${size}px; height: ${size}px; display: flex; align-items: center; justify-content: center; font-size: 10px; border: 1px solid #ccc;">Cargando QR...</div>`;
                }
            }

            function previewHTML() {
                const htmlContent = document.getElementById('htmlContent').value;
                const previewArea = document.getElementById('previewArea');
                const previewContent = document.getElementById('previewContent');
                
                if (!htmlContent.trim()) {
                    alert('No hay contenido HTML para mostrar');
                    return;
                }
                
                // Mostrar el HTML renderizado
                previewContent.innerHTML = htmlContent;
                previewArea.style.display = 'block';
                
                // Scroll hacia la vista previa
                previewArea.scrollIntoView({ behavior: 'smooth' });
            }

            function hidePreview() {
                document.getElementById('previewArea').style.display = 'none';
            }

            // Cargar impresoras al iniciar
            window.onload = function() {
                listPrinters();
                
                // Verificar si QRCode se cargó correctamente
                setTimeout(function() {
                    if (typeof QRCode === 'undefined') {
                        console.warn('QRCode library failed to load from CDN');
                        document.getElementById('qrStatus').innerHTML = '⚠️ Librería QR no disponible - usando generación del servidor';
                    } else {
                        console.log('QRCode library loaded successfully');
                        document.getElementById('qrStatus').innerHTML = 'Librería QR cargada correctamente';
                    }
                }, 1000);
            };
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/version", response_model=VersionResponse)
async def get_version():
    """Obtener información de versión del sistema"""
    app_info = config["app_info"]
    return VersionResponse(
        version=app_info["version"],
        name=app_info["name"]
    )

@app.post("/status", response_model=StatusResponse)
async def get_status(request: PrintRequest):
    """Obtener información de estado del sistema"""
    isOnline = get_status_printer(request.printer)
    if isOnline:
        status_msg = "Sistema operativo y servicios en funcionamiento"
    else:
        status_msg = "Sistema operativo en funcionamiento, pero algunos servicios pueden estar caídos"

    return StatusResponse(
        status=isOnline,
        message=status_msg
    )

@app.post("/cash_drawer", response_model=StatusResponse)
async def get_cash_drawer(request: PrintRequest):
    """Obtener información de estado del cajón de dinero"""
    isOpen = cash_drawer(request.printer)
    if isOpen:
        status_msg = "Cajón de dinero en línea"
    else:
        status_msg = "Cajón de dinero fuera de línea"

    return StatusResponse(
        status=isOpen,
        message=status_msg
    )


@app.post("/buzzer", response_model=StatusResponse)
async def get_buzzer(request: PrintRequest):
    """Obtener información de estado del buzzer"""
    isOn = buzzer(request.printer)
    if isOn:
        status_msg = "Buzzer activado"
    else:
        status_msg = "Buzzer desactivado"

    return StatusResponse(
        status=isOn,
        message=status_msg
    )

@app.options("/list_prints")
async def options_list_prints():
    """Manejar solicitudes OPTIONS para CORS"""
    return JSONResponse(
        content={"message": "OK"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "86400"
        }
    )

@app.get("/list_prints")
async def list_prints() -> List[PrinterInfo]:
    """Listar todas las impresoras disponibles"""
    all_printers = []
    
    # Obtener impresoras USB
    usb_printers = get_usb_printers()
    all_printers.extend(usb_printers)
    
    # Obtener impresoras de red
    network_printers = get_network_printers()
    all_printers.extend(network_printers)
    
    # Obtener impresoras del sistema
    system_printers = get_system_printers()
    all_printers.extend(system_printers)
    
    if not all_printers:
        # Si no se encuentran impresoras, devolver una lista con impresoras de ejemplo
        all_printers = [
            {
                "name": "Impresora_Virtual",
                "connection_type": "virtual",
                "status": "available",
                "description": "Impresora virtual para pruebas (no imprime realmente)",
                "puerto": "N/A"
            }
        ]
    
    return all_printers

@app.options("/send_printer")
async def options_send_printer():
    """Manejar solicitudes OPTIONS para CORS"""
    return JSONResponse(
        content={"message": "OK"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "86400"
        }
    )

@app.post("/send_printer", response_model=PrintResponse)
async def send_printer(request: PrintRequest):
    """Enviar contenido HTML a imprimir"""
    try:
        # Validar tamaño de papel
        if request.size not in ["80mm", "58mm"]:
            raise HTTPException(status_code=400, detail="Tamaño de papel debe ser '80mm' o '58mm'")
        
        # Validar que hay contenido HTML
        if not request.html.strip():
            raise HTTPException(status_code=400, detail="El contenido HTML no puede estar vacío")
                
        # Si es una impresora virtual (para pruebas)
        if request.printer == "Impresora_Virtual":
            logger.info(f"Impresión virtual - Contenido: {request.html[:100]}...")
            return PrintResponse(
                success=True,
                message="Impresión virtual exitosa (no se imprimió realmente)",
                printer_used=request.printer
            )
        
        # Determinar método de impresión basado en el tipo de impresora
        if "usb" in request.printer.lower():
            # Intentar impresión USB
            success, message = print_html(request.printer, request.html, request.size, request.font_size, request.test_width, request.line_spacing)
            
        elif "network" in request.printer.lower():
            # Intentar impresión de red
            success, message = print_html(request.printer, request.html, request.size, request.font_size, request.test_width, request.line_spacing)
            
        else:
            # Para impresoras del sistema, intentar múltiples métodos        
            logger.info(f"Intentando imprimir en impresora del sistema: {request.printer} (papel: {request.size}, fuente: {request.font_size})")
            methods_tried = []
            
            # Método 1: ESCPOS con Win32Raw (mejor para térmicas)
            try:
                # print(f"Método 1: ESCPOS con Win32Raw (papel: {request.size}, fuente: {request.font_size})")
                success, message = print_html(request.printer, request.html, request.size, request.font_size, request.test_width, request.line_spacing)
                # success, message = print_with_escpos_system(content, request.printer)
                methods_tried.append("ESCPOS")
                if success:
                    logger.info("Impresión ESCPOS exitosa")
            except Exception as e:
                logger.warning(f"Método ESCPOS falló: {e}")
                success = False
            
            # Método 2: Impresión RAW si ESCPOS falló (solo para contenido sin imágenes)
            if not success and WIN32_AVAILABLE:
                try:
                    # print(f"Método 2: Impresión RAW (papel: {request.size}, fuente: {request.font_size})")
                    success, message = print_html(request.printer, request.html, request.size, request.font_size, request.test_width, request.line_spacing)
                    methods_tried.append("RAW")
                    if success:
                        logger.info("Impresión RAW exitosa")
                except Exception as e:
                    logger.warning(f"Método RAW falló: {e}")
                    success = False
            
            # Método 3: Impresión del sistema si los anteriores fallaron (solo para contenido sin imágenes)
            if not success:
                try:
                    # print(f"Método 3: Impresión del sistema (papel: {request.size}, fuente: {request.font_size})")
                    success, message = print_html(request.printer, request.html, request.size, request.font_size, request.test_width)
                    methods_tried.append("Sistema")
                    if success:
                        logger.info("Impresión del sistema exitosa")
                except Exception as e:
                    logger.warning(f"Método sistema falló: {e}")
                    success = False
                    message = f"Error al imprimir en {request.printer}. Métodos probados: {', '.join(methods_tried)}. Último error: {str(e)}"
                        
        return PrintResponse(
            success=success,
            message=message,
            printer_used=request.printer if success else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en send_printer: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")


if __name__ == "__main__":
    try:
        import uvicorn
        host = config["api"]["host"]
        port = config["api"]["port"]
        debug = True if config["api"]["debug"] else False
        print(f"Iniciando PrintPOS API en http://{host}:{port}")
        print(f"Documentación disponible en http://{host}:{port}/docs")
        print(f"Interfaz web disponible en http://{host}:{port}")
        uvicorn.run(
            app,
            host=host,
            port=port,
            reload=debug,
            log_config=None
        )
    except Exception as e:
        import traceback
        print("ERROR AL INICIAR LA API:")
        traceback.print_exc()
        input("Presiona ENTER para cerrar...")
        exit(1)