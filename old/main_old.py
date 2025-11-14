from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import os
import platform
import subprocess
import re
from typing import List, Dict, Optional
import logging
from escpos.printer import Usb, Network, Dummy
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

# Modelos Pydantic
class PrintRequest(BaseModel):
    printer: str
    size: str  # "80mm" o "58mm"
    html: str

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

# Cargar configuración
def load_config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("config.json no encontrado")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Error al leer config.json: {e}")
        return None

config = load_config()
if not config:
    raise Exception("No se pudo cargar la configuración")

# Funciones auxiliares
def process_qr_codes_in_html(html_content: str) -> str:
    """Procesar elementos QR en HTML y reemplazarlos con imágenes generadas"""
    try:
        import re
        
        # Buscar elementos div con id que contengan "qr-"
        qr_pattern = r'<div id="(qr-[^"]+)"[^>]*>.*?</div>'
        
        def replace_qr(match):
            element_id = match.group(1)
            
            # Generar datos QR basados en el tipo
            if "qr-ticket" in element_id:
                qr_data = f"TICKET-{hash(html_content) % 10000}\nFecha: {import_datetime().datetime.now().strftime('%d/%m/%Y')}\nVerificar compra"
            elif "qr-receipt" in element_id:
                qr_data = f"COMPROBANTE-{hash(html_content) % 10000}\nPago de servicios\nVerificar pago"
            elif "qr-invoice" in element_id:
                qr_data = f"CFDI-UUID: {hash(html_content)}\nRFC: CEJ123456789\nVerificar factura"
            else:
                qr_data = f"Código QR - {element_id}"
            
            # Generar imagen QR
            qr_image = generate_qr_image(qr_data, (80, 80))
            
            if qr_image:
                # Convertir a base64
                qr_base64 = qr_to_base64(qr_image)
                if qr_base64:
                    return f'<img src="{qr_base64}" alt="QR Code" style="width:80px;height:80px;display:block;margin:5px auto;">'
            
            # Si falla, retornar texto alternativo
            return '<div style="text-align:center;border:1px solid #000;width:80px;height:80px;margin:5px auto;display:flex;align-items:center;justify-content:center;font-size:10px;">QR CODE</div>'
        
        # Reemplazar todos los elementos QR
        processed_html = re.sub(qr_pattern, replace_qr, html_content, flags=re.DOTALL)
        return processed_html
        
    except Exception as e:
        logger.error(f"Error procesando códigos QR: {e}")
        return html_content

def import_datetime():
    """Importar datetime de manera lazy"""
    import datetime
    return datetime
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

def html_to_printer_commands(html_content: str, paper_size: str):
    """Convertir HTML a comandos de impresora"""
    try:
        # Configuración de papel
        paper_config = config["paper_sizes"][paper_size]
        chars_per_line = paper_config["chars_per_line"]
        
        # Procesar códigos QR en el HTML antes de convertir a texto
        processed_html = process_qr_codes_in_html(html_content)

        # Marcar bloques centrados antes de convertir a texto plano
        def mark_centered_blocks(html):
            def replacer(match):
                tag = match.group(1)
                style = match.group(2)
                content = match.group(3)
                if 'text-align:center' in style.replace(' ', ''):
                    return f'<{tag} style="{style}">[CENTER]{content}[/CENTER]</{tag}>'
                    # return f'<{tag} style="{style}">{content}</{tag}>'
                elif 'align="center"' in style.replace(' ', ''):
                    return f'<{tag} style="{style}">[CENTER]{content}[/CENTER]</{tag}>'
                return match.group(0)
            pattern = r'<(div|p)[^>]*style\s*=\s*"([^"]+)"[^>]*>(.*?)</\1>'
            return re.sub(pattern, replacer, html, flags=re.DOTALL)

        marked_html = mark_centered_blocks(processed_html)

        # Convertir HTML a texto plano usando html2text
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = False  # Permitir imágenes para QR
        h.body_width = chars_per_line
        h.unicode_snob = True
        text_content = h.handle(marked_html)
        # print(f"{text_content}")
        # Procesar el texto para formato de ticket
        lines = text_content.split('\n')
        processed_lines = []

        for line in lines:
            print(f"Procesando línea: {line}")
            # Limpiar líneas vacías múltiples
            if line.strip() == '':
                print(f"if Procesando línea: {line}")
                if not processed_lines or processed_lines[-1] != '':
                    processed_lines.append('')
            
            # Si la línea contiene '[CENTER]' o '[/CENTER]', reemplazar y agregar los marcadores de alineación
            elif '[CENTER]' in line:
                new_line = line.replace('[CENTER]', '[ALIGN_CENTER]')
                processed_lines.append(new_line)
            elif '[/CENTER]' in line:
                new_line = line.replace('[/CENTER]', '[/ALIGN_CENTER]')
                processed_lines.append(new_line)
            else:
                print(f"else Procesando línea: {line}")
                # Si la línea está marcada como centrada
                if '[CENTER]' in line and '[/CENTER]' in line:
                    centered_text = line.replace('[CENTER]', '').replace('[/CENTER]', '').strip()
                    processed_lines.append(f'[ALIGN_CENTER]{centered_text}[/ALIGN_CENTER]')
                else:
                    # Ajustar línea si es muy larga
                    if len(line) > chars_per_line:
                        words = line.split()
                        current_line = ""
                        for word in words:
                            if len(current_line + " " + word) <= chars_per_line:
                                current_line += (" " + word) if current_line else word
                            else:
                                if current_line:
                                    processed_lines.append(current_line)
                                current_line = word
                        if current_line:
                            processed_lines.append(current_line)
                    else:
                        processed_lines.append(line)

        return '\n'.join(processed_lines)
        
    except Exception as e:
        logger.error(f"Error al procesar HTML: {e}")
        return html_content
    
    return html_content

def generate_qr_image(data: str, size: tuple = (100, 100)):
    """Generar imagen QR desde texto"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        # Crear imagen QR
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img = qr_img.resize(size, Image.Resampling.LANCZOS)
        
        return qr_img
    except Exception as e:
        logger.error(f"Error al generar QR: {e}")
        return None

def qr_to_base64(qr_image):
    """Convertir imagen QR a base64 para HTML"""
    try:
        buffered = BytesIO()
        qr_image.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        logger.error(f"Error al convertir QR a base64: {e}")
        return None

def extract_base64_images_from_html(html_content: str):
    """Extraer imágenes base64 del HTML y convertirlas para impresión"""
    import re
    
    images = []
    # Patrón para encontrar imágenes base64 en el HTML
    pattern = r'<img[^>]*src="data:image/[^;]+;base64,([^"]+)"[^>]*>'
    
    matches = re.finditer(pattern, html_content)
    for match in matches:
        try:
            base64_data = match.group(1)
            # Decodificar base64
            image_bytes = base64.b64decode(base64_data)
            image_stream = BytesIO(image_bytes)
            
            # Abrir imagen con PIL
            img = Image.open(image_stream)
            
            # Crear objeto EscposImage
            escpos_img = EscposImage(img)
            # Guardar también la imagen PIL original para compatibilidad
            escpos_img.pil_image = img
            images.append(escpos_img)
            
            logger.info(f"Imagen base64 procesada correctamente: {len(image_bytes)} bytes")
            
        except Exception as e:
            logger.error(f"Error procesando imagen base64: {e}")
            continue
    
    return images

def detect_and_process_base64_images(html_content: str):
    """Detectar imágenes base64 en HTML y procesarlas para impresión"""
    import re
    
    # Patrón para detectar div con imagen base64 y extraer estilos
    div_img_pattern = r'<div([^>]*)>(\s*)<img([^>]*)src="(data:image/[^;]+;base64,[^"]+)"([^>]*)>(.*?)</div>'
    images_found = []
    image_replacements = {}
    processed_html = html_content
    print(f"{processed_html}")

    # Buscar divs con imágenes base64
    for i, match in enumerate(re.finditer(div_img_pattern, html_content, re.DOTALL)):
        try:
            div_attrs = match.group(1)
            img_attrs = match.group(3) + match.group(5)
            full_base64_string = match.group(4)
            # Extraer estilos inline del div
            style_match = re.search(r'style\s*=\s*"([^"]+)"', div_attrs)
            div_style = style_match.group(1) if style_match else ""
            # Extraer estilos inline del img
            img_style_match = re.search(r'style\s*=\s*"([^"]+)"', img_attrs)
            img_style = img_style_match.group(1) if img_style_match else ""

            # Buscar width/height en el style del img
            width = None
            height = None
            if img_style:
                width_match = re.search(r'width\s*:\s*(\d+)px', img_style)
                height_match = re.search(r'height\s*:\s*(\d+)px', img_style)
                if width_match:
                    width = int(width_match.group(1))
                if height_match:
                    height = int(height_match.group(1))

            # Extraer solo la parte base64 (sin el prefijo data:image/...)
            base64_data = full_base64_string.split(',')[1]
            image_bytes = base64.b64decode(base64_data)
            image_stream = BytesIO(image_bytes)
            img = Image.open(image_stream)

            # Redimensionar si se especifica width/height
            if width or height:
                orig_w, orig_h = img.size
                new_w = width if width else orig_w
                new_h = height if height else orig_h
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            image_info = {
                'pil_image': img,
                'base64_string': full_base64_string,
                'position': i,
                'size': img.size,
                'div_style': div_style,
                'img_style': img_style,
                'width': width,
                'height': height
            }
            images_found.append(image_info)

            # Crear marcador único para reemplazar en el HTML
            marker = f"[IMG_PLACEHOLDER_{i}]"
            image_replacements[match.group(0)] = marker
            logger.info(f"Imagen base64 detectada #{i}: {img.size[0]}x{img.size[1]} px, estilos div: {div_style}, estilos img: {img_style}")
        except Exception as e:
            logger.error(f"Error procesando imagen base64 #{i}: {e}")
            continue

    # Reemplazar los divs con imágenes en el HTML por marcadores
    for div_html, marker in image_replacements.items():
        processed_html = processed_html.replace(div_html, marker)

    return processed_html, images_found

def print_content_with_images(content, images, printer_instance, paper_size: str = "80mm"):
    """Imprimir contenido de texto con imágenes intercaladas"""
    try:
        # Convertir contenido HTML a texto plano
        if isinstance(content, str) and ('<' in content or '>' in content):
            text_content = html_to_printer_commands(content, paper_size)
        else:
            text_content = str(content)
        
        lines = text_content.split('\n')
        image_index = 0

        # Patrón para extraer estilos inline de etiquetas HTML
                # Limpiar líneas vacías múltiples y procesar líneas

        for line in lines:
            # Buscar marcadores de imagen en la línea
            if '[IMG_PLACEHOLDER_' in line:
                marker_match = re.search(r'\[IMG_PLACEHOLDER_(\d+)\]', line)
                if marker_match:
                    placeholder_num = int(marker_match.group(1))
                    matching_image = None
                    for img_info in images:
                        if img_info['position'] == placeholder_num:
                            matching_image = img_info
                            break
                    if matching_image:
                        text_before = line[:marker_match.start()].strip()
                        if text_before:
                            printer_instance.text(text_before + '\n')
                        style = matching_image.get('div_style', '')
                        align = None
                        margin_top = 0
                        margin_bottom = 0
                        # Analizar estilos de alineación y margen
                        if 'text-align:' in style:
                            if 'center' in style:
                                align = 'center'
                            elif 'right' in style:
                                align = 'right'
                            elif 'left' in style:
                                align = 'left'
                        if 'margin-top:' in style:
                            try:
                                margin_top = int(re.search(r'margin-top:\s*(\d+)', style).group(1))
                            except:
                                margin_top = 0
                        if 'margin-bottom:' in style:
                            try:
                                margin_bottom = int(re.search(r'margin-bottom:\s*(\d+)', style).group(1))
                            except:
                                margin_bottom = 0
                        # Aplicar alineación
                        if align:
                            printer_instance.set(align=align)
                        for _ in range(margin_top // 5):
                            printer_instance.ln(1)
                        printer_instance.image(matching_image['pil_image'])
                        for _ in range(margin_bottom // 5):
                            printer_instance.ln(1)
                        if align:
                            printer_instance.set(align='left')
                        text_after = line[marker_match.end():].strip()
                        if text_after:
                            printer_instance.text(text_after + '\n')
                    else:
                        clean_line = re.sub(r'\[IMG_PLACEHOLDER_\d+\]', '[IMAGEN]', line)
                        if clean_line.strip():
                            printer_instance.text(clean_line + '\n')
                        else:
                            printer_instance.text('\n')
                else:
                    clean_line = re.sub(r'\[IMG_PLACEHOLDER_\d+\]', '[IMAGEN]', line)
                    if clean_line.strip():
                        printer_instance.text(clean_line + '\n')
                    else:
                        printer_instance.text('\n')
            # Detectar líneas marcadas para centrado
            elif '[ALIGN_CENTER]' in line and '[/ALIGN_CENTER]' in line:
                centered_text = line.replace('[ALIGN_CENTER]', '').replace('[/ALIGN_CENTER]', '').strip()
                printer_instance.set(align='center')
                printer_instance.text(centered_text + '\n')
                printer_instance.set(align='left')
            else:
                # Buscar estilos inline en la línea
                style_match = style_pattern.search(line)
                if style_match:
                    tag = style_match.group(1)
                    attrs = style_match.group(2)
                    style = style_match.group(3)
                    inner_text = style_match.group(4)
                    # Procesar estilos relevantes
                    align = None
                    font_size = None
                    font_weight = None
                    font_family = None
                    color = None
                    # Alineación
                    if 'text-align:' in style:
                        if 'center' in style:
                            align = 'center'
                        elif 'right' in style:
                            align = 'right'
                        elif 'left' in style:
                            align = 'left'
                    # Font size
                    font_size_match = re.search(r'font-size:\s*(\d+)px', style)
                    if font_size_match:
                        font_size = int(font_size_match.group(1))
                    # Font weight
                    if 'font-weight:' in style:
                        if 'bold' in style:
                            font_weight = 'bold'
                    # Font family
                    font_family_match = re.search(r'font-family:\s*([^;]+);?', style)
                    if font_family_match:
                        font_family = font_family_match.group(1).strip()
                    # Color
                    color_match = re.search(r'color:\s*([^;]+);?', style)
                    if color_match:
                        color = color_match.group(1).strip()
                    # Aplicar estilos compatibles con la impresora
                    if align:
                        printer_instance.set(align=align)
                    if font_weight == 'bold':
                        printer_instance.set(bold=True)
                    if font_size:
                        # Ajustar tamaño de fuente (solo 1=normal, 2=doble en la mayoría de impresoras)
                        if font_size >= 16:
                            printer_instance.set(width=2, height=2)
                        elif font_size >= 12:
                            printer_instance.set(width=1, height=1)
                    # Imprimir el texto con estilos
                    printer_instance.text(inner_text + '\n')
                    # Restaurar estilos
                    if align:
                        printer_instance.set(align='left')
                    if font_weight == 'bold':
                        printer_instance.set(bold=False)
                    if font_size:
                        printer_instance.set(width=1, height=1)
                else:
                    if line.strip():
                        printer_instance.text(line + '\n')
                    else:
                        printer_instance.text('\n')
        
        # Imprimir imágenes restantes al final si no fueron procesadas
        for img_info in images:
            if f"[IMG_PLACEHOLDER_{img_info['position']}]" not in text_content:
                printer_instance.ln(1)
                printer_instance.image(img_info['pil_image'])
                printer_instance.ln(1)
        
        logger.info(f"Impresión completada: {len(images)} imágenes procesadas")
        return True
        
    except Exception as e:
        logger.error(f"Error imprimiendo contenido con imágenes: {e}")
        return False

def print_to_usb(content, paper_size: str):
    """Imprimir usando USB"""
    try:
        usb_config = config["printers"]["usb"]
        vendor_id = int(usb_config["vendor_id"], 16)
        product_id = int(usb_config["product_id"], 16)
        
        printer = Usb(vendor_id, product_id)
        
        # Configurar papel
        printer.set(align='left', width=1, height=1, font='a')
        
        if isinstance(content, Image.Image):
            # Si es una imagen PIL directa
            escpos_img = EscposImage(content)
            printer.image(escpos_img)
        elif isinstance(content, str) and 'data:image' in content:
            # Detectar y procesar imágenes base64 en HTML
            processed_html, images = detect_and_process_base64_images(content)
            
            if images:
                logger.info(f"Detectadas {len(images)} imágenes base64 en el HTML")
                # Imprimir contenido con imágenes intercaladas
                success = print_content_with_images(processed_html, images, printer, paper_size)
                if not success:
                    # Fallback: imprimir texto y luego imágenes
                    text_content = html_to_printer_commands(processed_html, paper_size)
                    lines = str(text_content).split('\n')
                    for line in lines:
                        if line.strip():
                            printer.text(line + '\n')
                        else:
                            printer.text('\n')
                    
                    # Imprimir imágenes al final
                    for img_info in images:
                        printer.ln(1)
                        printer.image(img_info['pil_image'])
                        printer.ln(1)
            else:
                # No hay imágenes, procesar como HTML normal
                text_content = html_to_printer_commands(content, paper_size)
                lines = str(text_content).split('\n')
                for line in lines:
                    if line.strip():
                        printer.text(line + '\n')
                    else:
                        printer.text('\n')
        else:
            # Imprimir texto normal línea por línea
            lines = str(content).split('\n')
            for line in lines:
                if line.strip():
                    printer.text(line + '\n')
                else:
                    printer.text('\n')
        
        printer.ln(2)  # Salto de línea adicional
        printer.cut()
        printer.close()
        return True, "Impresión USB exitosa"
        
    except USBNotFoundError:
        return False, "Impresora USB no encontrada - Verificar conexión y drivers"
    except Exception as e:
        logger.error(f"Error detallado USB: {e}")
        return False, f"Error en impresión USB: {str(e)}"

def print_to_network(content, paper_size: str):
    """Imprimir usando red"""
    try:
        network_config = config["printers"]["network"]
        ip = network_config["ip"]
        port = network_config.get("port", 9100)
        
        printer = Network(ip, port)
        
        # Configurar papel
        printer.set(align='left', width=1, height=1, font='a')
        
        if isinstance(content, Image.Image):
            # Si es una imagen PIL directa
            escpos_img = EscposImage(content)
            printer.image(escpos_img)
        elif isinstance(content, str) and 'data:image' in content:
            # Detectar y procesar imágenes base64 en HTML
            processed_html, images = detect_and_process_base64_images(content)
            
            if images:
                logger.info(f"Detectadas {len(images)} imágenes base64 en el HTML")
                # Imprimir contenido con imágenes intercaladas
                success = print_content_with_images(processed_html, images, printer, paper_size)
                if not success:
                    # Fallback: imprimir texto y luego imágenes
                    text_content = html_to_printer_commands(processed_html, paper_size)
                    lines = str(text_content).split('\n')
                    for line in lines:
                        if line.strip():
                            printer.text(line + '\n')
                        else:
                            printer.text('\n')
                    
                    # Imprimir imágenes al final
                    for img_info in images:
                        printer.ln(1)
                        printer.image(img_info['pil_image'])
                        printer.ln(1)
            else:
                # No hay imágenes, procesar como HTML normal
                text_content = html_to_printer_commands(content, paper_size)
                lines = str(text_content).split('\n')
                for line in lines:
                    if line.strip():
                        printer.text(line + '\n')
                    else:
                        printer.text('\n')
        else:
            # Imprimir texto normal línea por línea
            lines = str(content).split('\n')
            for line in lines:
                if line.strip():
                    printer.text(line + '\n')
                else:
                    printer.text('\n')
        
        printer.ln(2)  # Salto de línea adicional
        printer.cut()
        printer.close()
        return True, "Impresión de red exitosa"
        
    except Exception as e:
        logger.error(f"Error detallado Red: {e}")
        return False, f"Error en impresión de red: {str(e)}"

def print_to_system_printer(content, paper_size: str, printer_name: str):
    """Imprimir usando impresora del sistema Windows"""
    if not WIN32_AVAILABLE:
        return False, "win32print no está disponible"
    
    try:
        # Crear archivo temporal con el contenido
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(str(content))
            temp_file_path = temp_file.name
        
        # Imprimir usando win32print
        try:
            # Abrir la impresora
            printer_handle = win32print.OpenPrinter(printer_name)
            
            # Iniciar documento de impresión
            doc_info = ("Ticket POS", None, "TEXT")
            job_id = win32print.StartDocPrinter(printer_handle, 1, doc_info)
            
            # Iniciar página
            win32print.StartPagePrinter(printer_handle)
            
            # Leer contenido del archivo
            with open(temp_file_path, 'r', encoding='utf-8') as f:
                content_str = f.read()
            
            # Escribir datos a la impresora
            win32print.WritePrinter(printer_handle, content_str.encode('utf-8'))
            
            # Finalizar página y documento
            win32print.EndPagePrinter(printer_handle)
            win32print.EndDocPrinter(printer_handle)
            win32print.ClosePrinter(printer_handle)
            
            # Limpiar archivo temporal
            import os
            try:
                os.unlink(temp_file_path)
            except:
                pass
            
            return True, f"Impresión exitosa en {printer_name}"
            
        except Exception as e:
            logger.error(f"Error win32print: {e}")
            # Fallback: usar comando print de Windows
            try:
                import subprocess
                import os
                
                # Usar el comando print de Windows
                result = subprocess.run([
                    'cmd', '/c', f'type "{temp_file_path}" > PRN'
                ], capture_output=True, text=True, check=False)
                
                # Limpiar archivo temporal
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
                
                if result.returncode == 0:
                    return True, f"Documento enviado a imprimir en {printer_name}"
                else:
                    return False, f"Error en comando print: {result.stderr}"
            except Exception as e2:
                return False, f"Error en impresión del sistema: {str(e2)}"
        
    except Exception as e:
        logger.error(f"Error sistema: {e}")
        return False, f"Error en impresión del sistema: {str(e)}"

def print_raw_to_printer(content, printer_name: str):
    """Enviar datos RAW directamente a la impresora"""
    if not WIN32_AVAILABLE:
        return False, "win32print no está disponible para impresión RAW"
    
    try:
        # Para impresoras térmicas, agregar comandos ESC/POS básicos
        esc_init = b"\x1B\x40"  # Initialize printer
        content_bytes = str(content).encode('utf-8', errors='ignore')
        esc_feed = b"\x1B\x64\x02"  # Feed 2 lines
        esc_cut = b"\x1D\x56\x41\x10"  # Cut paper
        
        full_content = esc_init + content_bytes + esc_feed + esc_cut
        
        # Enviar a impresora
        printer_handle = win32print.OpenPrinter(printer_name)
        job_info = ("Ticket POS RAW", None, "RAW")
        job_id = win32print.StartDocPrinter(printer_handle, 1, job_info)
        win32print.StartPagePrinter(printer_handle)
        win32print.WritePrinter(printer_handle, full_content)
        win32print.EndPagePrinter(printer_handle)
        win32print.EndDocPrinter(printer_handle)
        win32print.ClosePrinter(printer_handle)
        
        return True, f"Datos RAW enviados a {printer_name}"
        
    except Exception as e:
        logger.error(f"Error RAW: {e}")
        return False, f"Error en impresión RAW: {str(e)}"

def print_with_escpos_system(content, printer_name: str):
    """Imprimir usando escpos con impresora del sistema"""
    print(f"Imprimir usando escpos con impresora del sistema: {printer_name}")
    try:
        from escpos import printer
        
        # Intentar usar Win32Raw si está disponible
        if WIN32_AVAILABLE:
            try:
                p = printer.Win32Raw(printer_name)
                
                if isinstance(content, Image.Image):
                    # Si es una imagen PIL directa, pasarla directamente
                    p.image(content)
                elif isinstance(content, str) and 'data:image' in content:
                    # Detectar y procesar imágenes base64 en HTML
                    processed_html, images = detect_and_process_base64_images(content)
                    
                    if images:
                        logger.info(f"Detectadas {len(images)} imágenes base64 para impresora del sistema")
                        # Imprimir contenido con imágenes intercaladas usando Win32Raw
                        print("Imprimir contenido con imágenes intercaladas usando Win32Raw")
                        success = print_content_with_images(processed_html, images, p, "80mm")
                        if not success:
                            # Fallback: imprimir texto y luego imágenes
                            p.text(str(processed_html))
                            for img_info in images:
                                p.ln(1)
                                p.image(img_info['pil_image'])
                                p.ln(1)
                    else:
                        # No hay imágenes, imprimir texto normal
                        p.text(str(content))
                else:
                    # Texto normal
                    # print(f"{content}")
                    p.text(str(content))
                
                # p.ln(2)
                p.cut()
                p.close()
                print(f"Impresión ESCPOS exitosa en {printer_name}")
                return True, f"Impresión ESCPOS exitosa en {printer_name}"
            except Exception as e:
                logger.warning(f"Win32Raw falló: {e}")
        
        # Fallback: usar Dummy printer para debug
        p = printer.Dummy()
        if isinstance(content, str) and 'data:image' in content:
            processed_html, images = detect_and_process_base64_images(content)
            p.text(str(processed_html))
            for img_info in images:
                # Para Dummy printer, crear EscposImage
                escpos_img = EscposImage(img_info['pil_image'])
                p.image(escpos_img)
        else:
            p.text(str(content))
        p.cut()
        output = p.output
        print(f"Contenido que se enviaría a imprimir: {output[:200]}...")
        logger.info(f"Contenido que se enviaría a imprimir: {output[:200]}...")
        return True, f"Simulación de impresión en {printer_name} (modo debug)"
        
    except Exception as e:
        logger.error(f"Error ESCPOS sistema: {e}")
        return False, f"Error en impresión ESCPOS: {str(e)}"

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
                <h1>🖨️ PrintPOS API</h1>
                <p>API para impresión de tickets POS en impresoras térmicas</p>
            </div>

            <div class="grid">
                <!-- Sección de información -->
                <div class="section">
                    <h3>📋 Información del Sistema</h3>
                    <button onclick="getVersion()">Obtener Versión</button>
                    <div id="versionResponse" class="response info"></div>
                    
                    <button onclick="listPrinters()">Listar Impresoras</button>
                    <div id="printersResponse" class="response info"></div>
                </div>

                <!-- Sección de impresión -->
                <div class="section">
                    <h3>🖨️ Enviar a Imprimir</h3>
                    <div class="form-group">
                        <label for="printerSelect">Seleccionar Impresora:</label>
                        <select id="printerSelect">
                            <option value="">Cargar impresoras primero...</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label for="paperSize">Tamaño de Papel:</label>
                        <select id="paperSize">
                            <option value="80mm">80mm</option>
                            <option value="58mm">58mm</option>
                        </select>
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
        <tr style="border-top: 1px solid black;">
            <td><strong>TOTAL:</strong></td>
            <td style="text-align: right;"><strong>$25.50</strong></td>
        </tr>
    </table>
    <p>================================</p>
    <p>¡Gracias por su compra!</p>
    <p>Vuelva pronto</p>
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

            <!-- Sección de ejemplos -->
            <div class="section">
                <h3>📝 Ejemplos de HTML</h3>
                <div id="qrStatus" style="font-size: 12px; color: #666; margin-bottom: 10px;">⏳ Verificando librería QR...</div>
                <button onclick="loadExample('ticket')">🎫 Ticket de Venta</button>
                <button onclick="loadExample('receipt')">🧾 Recibo</button>
                <button onclick="loadExample('invoice')">📄 Factura Simple</button>
                <br><br>
                <button onclick="testQRSimple()" style="background-color: #28a745; color: white;">🧪 Test QR Simple</button>
                <button onclick="testImagePrint()" style="background-color: #dc3545; color: white;">🖨️ Test Impresión Imagen</button>
                <button onclick="addQRToCurrentHTML()" class="btn-secondary" style="font-size: 12px;">📱 Agregar QR al HTML Actual</button>
                <br><br>
                <div style="border-top: 1px solid #ddd; padding-top: 15px;">
                    <h4>📱 Generar QR Personalizado</h4>
                    <input type="text" id="qrData" placeholder="Ingrese datos para el QR (ej: https://miempresa.com)" style="margin-bottom: 10px;">
                    <button onclick="generateCustomQR()" class="btn-secondary">🔗 Generar QR</button>
                    <div id="customQRResult" style="text-align: center; margin-top: 10px;"></div>
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
                element.className = `response ${isSuccess ? 'success' : 'error'}`;
                element.innerHTML = `<pre>${JSON.stringify(response, null, 2)}</pre>`;
            }

            async function getVersion() {
                const result = await makeRequest('/version');
                displayResponse('versionResponse', result.data || result.error, result.success);
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

                if (!printer) {
                    alert('Por favor seleccione una impresora');
                    return;
                }

                if (!html.trim()) {
                    alert('Por favor ingrese contenido HTML');
                    return;
                }

                const printData = {
                    printer: printer,
                    size: size,
                    html: html
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

            async function addQRToExample(type) {
                console.log('Iniciando generación de QR para:', type);
                
                try {
                    // Datos simples para el QR
                    const qrData = `Ejemplo ${type.toUpperCase()} - Fecha: ${new Date().toLocaleDateString()} - ID: ${Math.floor(Math.random() * 1000)}`;
                    
                    console.log('Datos del QR:', qrData);

                    // Llamar al servidor para generar QR
                    const response = await fetch('/generate_qr', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ data: qrData, size: 80 })
                    });

                    console.log('Respuesta del servidor:', response.status);
                    const result = await response.json();
                    console.log('Resultado:', result);

                    if (result.success && result.qr_code) {
                        // Obtener HTML actual
                        const currentHTML = document.getElementById('htmlContent').value;
                        
                        // Crear HTML del QR muy simple
                        const qrHTML = `
    <p>================================</p>
    <p style="text-align: center;"><strong>CÓDIGO QR</strong></p>
    <div style="text-align: center; margin: 10px 0;">
        <img src="${result.qr_code}" style="width: 300px; height: 300px; border: 1px solid black;">
    </div>
    <p style="text-align: center; font-size: 10px;">Escanea para verificar</p>`;
                        
                        // Agregar QR al HTML
                        document.getElementById('htmlContent').value = currentHTML + qrHTML;
                        console.log('✅ QR agregado exitosamente');
                        
                    } else {
                        console.error('❌ Error en respuesta:', result);
                        alert('Error al generar QR: ' + (result.error || 'Desconocido'));
                    }
                } catch (error) {
                    console.error('❌ Error de red:', error);
                    alert('Error de red: ' + error.message);
                }
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

                if (qrData && elementId) {
                    try {
                        const response = await makeRequest('/generate_qr', {
                            method: 'POST',
                            body: JSON.stringify({
                                data: qrData,
                                size: size
                            })
                        });

                        if (response.success && response.data && response.data.qr_code) {
                            const element = document.getElementById(elementId);
                            if (element) {
                                element.innerHTML = `<img src="${response.data.qr_code}" alt="QR Code" style="width:${size}px;height:${size}px;">`;
                            }
                        }
                    } catch (error) {
                        console.error('Error generating server-side QR:', error);
                    }
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

            async function generateCustomQR() {
                const qrData = document.getElementById('qrData').value.trim();
                const resultDiv = document.getElementById('customQRResult');
                
                if (!qrData) {
                    alert('Por favor ingrese datos para el código QR');
                    return;
                }

                // Mostrar indicador de carga
                resultDiv.innerHTML = '<p>🔄 Generando QR...</p>';

                try {
                    // Siempre usar generación del servidor para QR personalizados
                    const response = await makeRequest('/generate_qr', {
                        method: 'POST',
                        body: JSON.stringify({
                            data: qrData,
                            size: 120
                        })
                    });

                    if (response.success && response.data && response.data.qr_code) {
                        resultDiv.innerHTML = `
                            <div style="margin: 10px 0;">
                                <p><strong>✅ QR Generado</strong></p>
                                <img src="${response.data.qr_code}" alt="QR Code" style="max-width: 120px; border: 1px solid #ddd; display: block; margin: 10px auto;">
                                <br>
                                <button onclick="insertQRIntoHTML('${response.data.qr_code}')" style="margin-top: 10px; padding: 5px 10px;">📝 Insertar en HTML</button>
                            </div>
                        `;
                    } else {
                        resultDiv.innerHTML = '<p style="color: red;">❌ Error al generar QR: ' + (response.error || 'Error desconocido') + '</p>';
                    }
                } catch (error) {
                    console.error('Error:', error);
                    resultDiv.innerHTML = '<p style="color: red;">❌ Error al generar QR: ' + error.message + '</p>';
                }
            }

            function insertQRIntoHTML(qrBase64) {
                const htmlContent = document.getElementById('htmlContent');
                const qrHTML = `
    <div style="text-align: center; margin: 10px 0;">
        <p><strong>📱 Código QR</strong></p>
        <img src="${qrBase64}" alt="QR Code" style="width: 100px; height: 100px;">
        <p style="font-size: 10px;">Escanea para más información</p>
    </div>`;
                
                // Insertar QR al final del contenido actual
                htmlContent.value += qrHTML;
                
                // Mostrar mensaje de confirmación
                document.getElementById('customQRResult').innerHTML += '<p style="color: green; margin-top: 10px;">✅ QR insertado en el HTML</p>';
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

            async function addQRToCurrentHTML() {
                const htmlContent = document.getElementById('htmlContent').value.trim();
                
                if (!htmlContent) {
                    alert('Por favor ingrese contenido HTML primero');
                    return;
                }

                const qrData = prompt('Ingrese los datos para el código QR:', 'Mi empresa - Tel: 123-456-7890');
                
                if (!qrData) {
                    return;
                }

                console.log('Agregando QR con datos:', qrData);

                try {
                    // Generar QR usando el servidor
                    const response = await makeRequest('/generate_qr', {
                        method: 'POST',
                        body: JSON.stringify({
                            data: qrData,
                            size: 100
                        })
                    });

                    if (response.success && response.data && response.data.qr_code) {
                        // Crear sección QR
                        const qrSection = `
    <div style="text-align: center; margin: 15px 0; padding: 10px; border: 1px solid #ddd;">
        <p><strong>📱 Código QR</strong></p>
        <img src="${response.data.qr_code}" alt="QR Code" style="width:100px;height:100px;display:block;margin:10px auto;border:1px solid #ccc;">
        <p style="font-size: 10px;">Escanea para más información</p>
    </div>`;
                        
                        // Agregar QR al final del HTML actual
                        document.getElementById('htmlContent').value = htmlContent + qrSection;
                        
                        alert('✅ QR agregado exitosamente al HTML');
                        console.log('✅ QR agregado al HTML');
                    } else {
                        alert('❌ Error al generar QR: ' + (response.error || 'Error desconocido'));
                    }
                } catch (error) {
                    console.error('Error agregando QR:', error);
                    alert('❌ Error al agregar QR: ' + error.message);
                }
            }

            async function testQRSimple() {
                console.log('🧪 Iniciando test de QR simple...');
                
                try {
                    // Test muy simple
                    const testData = 'TEST QR - ' + new Date().toLocaleString();
                    console.log('Datos de test:', testData);
                    
                    const response = await fetch('/generate_qr', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ data: testData, size: 120 })
                    });
                    
                    console.log('Respuesta del servidor:', response.status);
                    const result = await response.json();
                    console.log('Resultado del test:', result);
                    
                    if (result.success && result.qr_code) {
                        // HTML de test muy simple
                        const testHTML = `<div style="text-align: center; padding: 20px;">
    <h2>🧪 TEST QR SIMPLE</h2>
    <p>Si ves una imagen QR abajo, funciona correctamente:</p>
    <img src="${result.qr_code}" alt="QR de Test" style="width: 120px; height: 120px; border: 2px solid green; margin: 10px;">
    <p style="font-size: 12px;">Datos del QR: ${testData}</p>
</div>`;
                        
                        document.getElementById('htmlContent').value = testHTML;
                        alert('✅ Test QR completado. Revisa el HTML para ver si aparece la imagen.');
                    } else {
                        alert('❌ Test QR falló: ' + (result.error || 'Error desconocido'));
                    }
                } catch (error) {
                    console.error('Error en test QR:', error);
                    alert('❌ Error en test QR: ' + error.message);
                }
            }

            async function testImagePrint() {
                const printer = document.getElementById('printerSelect').value;
                
                if (!printer) {
                    alert('Por favor seleccione una impresora primero');
                    return;
                }
                
                console.log('🖨️ Iniciando test de impresión de imagen...');
                
                try {
                    // Primero generar un QR de prueba
                    const testData = 'TEST IMPRESIÓN - ' + new Date().toLocaleString();
                    
                    const qrResponse = await fetch('/generate_qr', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ data: testData, size: 120 })
                    });
                    
                    const qrResult = await qrResponse.json();
                    
                    if (qrResult.success && qrResult.qr_code) {
                        console.log('QR generado, enviando a impresora...');
                        
                        // Enviar imagen a impresora
                        const printResponse = await fetch('/test_image_print', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                printer: printer,
                                base64_image: qrResult.qr_code
                            })
                        });
                        
                        const printResult = await printResponse.json();
                        
                        if (printResult.success) {
                            alert('✅ Test de impresión de imagen exitoso!\\n' + printResult.message);
                        } else {
                            alert('❌ Error en test de impresión: ' + (printResult.error || printResult.message));
                        }
                    } else {
                        alert('❌ Error generando QR para test: ' + (qrResult.error || 'Error desconocido'));
                    }
                } catch (error) {
                    console.error('Error en test de impresión:', error);
                    alert('❌ Error en test de impresión: ' + error.message);
                }
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
                        document.getElementById('qrStatus').innerHTML = '✅ Librería QR cargada correctamente';
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
        
        # Detectar si hay imágenes base64 en el HTML
        has_base64_images = 'data:image' in request.html
        if has_base64_images:
            logger.info("HTML contiene imágenes base64 - procesando para impresión")
            # Usar el HTML directamente con las imágenes base64
            content = request.html
        else:
            # Procesar el contenido HTML normalmente
            content = html_to_printer_commands(request.html, request.size)
        
        # Determinar tipo de impresora y enviar a imprimir
        success = False
        message = ""
        
        # Si es una impresora virtual (para pruebas)
        if request.printer == "Impresora_Virtual":
            if has_base64_images:
                # Simular procesamiento de imágenes
                processed_html, images = detect_and_process_base64_images(request.html)
                logger.info(f"Impresión virtual - Contenido con {len(images)} imágenes: {processed_html[:100]}...")
                return PrintResponse(
                    success=True,
                    message=f"Impresión virtual exitosa con {len(images)} imágenes base64 (no se imprimió realmente)",
                    printer_used=request.printer
                )
            else:
                logger.info(f"Impresión virtual - Contenido: {request.html[:100]}...")
                return PrintResponse(
                    success=True,
                    message="Impresión virtual exitosa (no se imprimió realmente)",
                    printer_used=request.printer
                )
        
        # Determinar método de impresión basado en el tipo de impresora
        if "usb" in request.printer.lower():
            # Intentar impresión USB
            success, message = print_to_usb(content, request.size)
            
        elif "network" in request.printer.lower():
            # Intentar impresión de red
            success, message = print_to_network(content, request.size)
            
        else:
            # Para impresoras del sistema, intentar múltiples métodos
            logger.info(f"Intentando imprimir en impresora del sistema: {request.printer}")
            
            methods_tried = []
            
            # Método 1: ESCPOS con Win32Raw (mejor para térmicas)
            try:
                print("Método 1: ESCPOS con Win32Raw (mejor para térmicas)")
                success, message = print_with_escpos_system(content, request.printer)
                methods_tried.append("ESCPOS")
                if success:
                    logger.info("Impresión ESCPOS exitosa")
            except Exception as e:
                logger.warning(f"Método ESCPOS falló: {e}")
                success = False
            
            # Método 2: Impresión RAW si ESCPOS falló (solo para contenido sin imágenes)
            if not success and WIN32_AVAILABLE and not has_base64_images:
                try:
                    print("Método 2: Impresión RAW (solo para contenido sin imágenes)")
                    success, message = print_raw_to_printer(content, request.printer)
                    methods_tried.append("RAW")
                    if success:
                        logger.info("Impresión RAW exitosa")
                except Exception as e:
                    logger.warning(f"Método RAW falló: {e}")
                    success = False
            
            # Método 3: Impresión del sistema si los anteriores fallaron (solo para contenido sin imágenes)
            if not success and not has_base64_images:
                try:
                    print("Método 3: Impresión del sistema (solo para contenido sin imágenes)")
                    success, message = print_to_system_printer(content, request.size, request.printer)
                    methods_tried.append("Sistema")
                    if success:
                        logger.info("Impresión del sistema exitosa")
                except Exception as e:
                    logger.warning(f"Método sistema falló: {e}")
                    success = False
                    message = f"Error al imprimir en {request.printer}. Métodos probados: {', '.join(methods_tried)}. Último error: {str(e)}"
        
        # Agregar información sobre imágenes procesadas al mensaje
        if success and has_base64_images:
            print("Imágenes procesadas:")
            processed_html, images = detect_and_process_base64_images(request.html)
            if images:
                message += f" - {len(images)} imágenes base64 procesadas"
        
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

# Modelo para generar QR
class QRRequest(BaseModel):
    data: str
    size: Optional[int] = 100

# Modelo para procesar HTML con QR
class ProcessHTMLRequest(BaseModel):
    html: str

# Modelo para test de impresión de imágenes
class TestImagePrintRequest(BaseModel):
    printer: str
    base64_image: str

@app.post("/test_image_print")
async def test_image_print(request: TestImagePrintRequest):
    """Endpoint para probar impresión de imágenes base64"""
    try:
        # Decodificar imagen base64
        if request.base64_image.startswith('data:image'):
            # Remover el prefijo data:image/...;base64,
            base64_data = request.base64_image.split(',')[1]
        else:
            base64_data = request.base64_image
            
        image_bytes = base64.b64decode(base64_data)
        image_stream = BytesIO(image_bytes)
        
        # Abrir imagen con PIL
        img = Image.open(image_stream)
        
        # Imprimir según el tipo de impresora
        if "usb" in request.printer.lower():
            success, message = print_to_usb(img, "80mm")
        elif "network" in request.printer.lower():
            success, message = print_to_network(img, "80mm")
        else:
            success, message = print_with_escpos_system(img, request.printer)
            
        return {
            "success": success,
            "message": message,
            "image_size": f"{img.size[0]}x{img.size[1]}"
        }
        
    except Exception as e:
        logger.error(f"Error en test de impresión de imagen: {e}")
        return {
            "success": False,
            "error": f"Error al procesar imagen: {str(e)}"
        }

@app.post("/process_html_qr")
async def process_html_qr_endpoint(request: ProcessHTMLRequest):
    """Procesar HTML y convertir elementos QR a imágenes"""
    try:
        processed_html = process_qr_codes_in_html(request.html)
        return {
            "success": True,
            "processed_html": processed_html,
            "message": "HTML procesado exitosamente"
        }
    except Exception as e:
        logger.error(f"Error procesando HTML con QR: {e}")
        raise HTTPException(status_code=500, detail=f"Error al procesar HTML: {str(e)}")

@app.get("/test_qr_simple.html")
async def serve_test_qr():
    """Servir archivo de test QR"""
    file_path = "test_qr_simple.html"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="text/html")
    else:
        raise HTTPException(status_code=404, detail="Archivo de test no encontrado")

@app.post("/generate_qr")
async def generate_qr_endpoint(request: QRRequest):
    """Generar código QR y devolver como imagen base64"""
    try:
        # Generar imagen QR
        qr_image = generate_qr_image(request.data, (request.size, request.size))
        
        if not qr_image:
            raise HTTPException(status_code=500, detail="Error al generar código QR")
        
        # Convertir a base64
        qr_base64 = qr_to_base64(qr_image)
        
        if not qr_base64:
            raise HTTPException(status_code=500, detail="Error al convertir QR a base64")
        
        return {
            "success": True,
            "qr_code": qr_base64,
            "message": "Código QR generado exitosamente"
        }
        
    except Exception as e:
        logger.error(f"Error generando QR: {e}")
        raise HTTPException(status_code=500, detail=f"Error al generar QR: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    
    host = config["api"]["host"]
    port = config["api"]["port"]
    debug = config["api"]["debug"]
    
    print(f"🚀 Iniciando PrintPOS API en http://{host}:{port}")
    print(f"📖 Documentación disponible en http://{host}:{port}/docs")
    print(f"🖨️ Interfaz web disponible en http://{host}:{port}")
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=debug
    )