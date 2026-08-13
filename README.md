# Asistente de Revisión Científica — MVP Doma

Este proyecto implementa un prototipo local en **Python + Streamlit** para acelerar la revisión de literatura científica sin sustituir la evaluación profesional. Integra un detector de alertas, un extractor de hallazgos con citas literales y un panel con decisión humana e historial persistente.

> **Regla de oro:** la aplicación solo presenta evidencias y sugerencias. No aprueba ni rechaza ningún documento de forma automática; una persona revisora registra la única decisión válida.

| Módulo | Qué hace | Salvaguarda |
|---|---|---|
| **Detector** | Contrasta DOI/PMID, metadatos y posibles estados de retractación, corrección o preocupación editorial. | Una alerta es una señal para revisar, nunca un veredicto. |
| **Extractor** | Lee el PDF, muestra fragmentos literales con página y puede organizarlos en hallazgos sugeridos mediante una API opcional. | Las citas provienen del PDF local y se validan antes de mostrarse. Cuando existe duda, se marca como **Pendiente de revisión**. |
| **Panel humano** | Clasifica por tema, registra **Pendiente**, **Aprobar** o **Rechazar**, exige justificación y conserva historial en SQLite. | El sistema no puede crear una decisión sin nombre de revisor y justificación. |

## 1. Ubicación solicitada e instalación en Windows

Descomprima o copie la carpeta `proyec` en la ruta solicitada:

```text
C:\Users\herre\visual\proyec
```

Abra **PowerShell** o el terminal de Visual Studio Code dentro de esa carpeta y ejecute los comandos siguientes. Se recomienda Python 3.10 o superior.

```powershell
cd C:\Users\herre\visual\proyec
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Si PowerShell bloquea la activación del entorno virtual, ejecute una vez `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` y vuelva a ejecutar el comando de activación. También puede usar Símbolo del sistema con `.venv\Scripts\activate.bat`.

## 2. Configuración segura de la API opcional

La API solo se usa para **organizar sugerencias de hallazgos**. Si no se configura, la aplicación sigue operando con fragmentos textuales del PDF, pero no produce una síntesis asistida.

Por seguridad, la clave que se compartió en el encargo **no está incluida** en el código ni en este paquete. Cree su archivo local `.env` desde la plantilla:

```powershell
Copy-Item .env.example .env
notepad .env
```

## 3. Iniciar la aplicación

Con el entorno virtual activo, ejecute:

```powershell
streamlit run app.py
```

El navegador se abrirá normalmente en `http://localhost:8501`. Para detener la aplicación, pulse `Ctrl + C` en el terminal.

| Entrada permitida | Flujo resultante |
|---|---|
| **PDF local** | Extrae texto por página, detecta DOI/PMID y habilita detector, extractor y visor del documento. |
| **URL directa a PDF** | Descarga el PDF con un límite configurable de tamaño y lo procesa localmente. |
| **DOI** | Consulta metadatos y señales públicas; no muestra citas porque no tiene el PDF. |
| **PMID o enlace PubMed** | Consulta el registro PubMed; no muestra citas si no se adjunta el PDF. |

## 4. Guion de demo para el hackathon

1. Suba un PDF científico o pegue un DOI y pulse **Analizar documento**.
2. En la pestaña **Detector**, muestre el DOI, los metadatos y las alertas explicables. Abra las fuentes primarias enlazadas cuando exista una señal.
3. En **Extractor**, muestre que cada cita incluye su texto literal y el número de página. Explique que el estado puede ser **Pendiente de revisión** y que la aplicación no inventa citas.
4. En **Panel humano**, clasifique el documento —por ejemplo, `Sueño` o `Ansiedad`—, elija una decisión humana y escriba una justificación.
5. En **Historial**, muestre el registro auditable y exporte el CSV si fuera necesario.

## 5. Fuentes consultadas por el detector

Crossref ofrece datos de Retraction Watch en su API REST y describe que las retractaciones y otras actualizaciones aparecen en el campo `update-to`; por ello el MVP usa ese campo como una **señal de revisión** y no como una decisión final. [1] Las E-utilities de NCBI permiten realizar búsquedas y recuperaciones programáticas de registros PubMed, por lo que el proyecto consulta sus tipos de publicación y relaciones editoriales cuando encuentra un DOI o PMID. [2] OpenAlex se emplea como contraste adicional cuando identifica la obra por DOI.

| Fuente | Comprobación aplicada | Límites que debe considerar el revisor |
|---|---|---|
| Crossref / Retraction Watch | Actualizaciones `update-to` relacionadas con retractación o retiro. | La recuperación de metadatos puede ser incompleta o tardar en reflejar cambios. |
| PubMed / NCBI | Tipos de publicación y vínculos de corrección/retractación. | No todos los artículos están indexados en PubMed. |
| OpenAlex | Campo `is_retracted` cuando se localiza la obra. | Debe validarse en el aviso editorial original. |
| PDF adjunto | Texto, DOI, PMID, citas y páginas. | Los PDF escaneados sin capa de texto requieren OCR externo. |

## 6. Estructura del proyecto

```text
proyec/
├── app.py                       # Interfaz Streamlit
├── requirements.txt             # Dependencias
├── .env.example                 # Plantilla de configuración privada
├── .gitignore                   # Evita exponer secretos y datos locales
├── run_windows.bat              # Inicio rápido para Windows
├── src/
│   ├── config.py                # Parámetros y límites
│   ├── database.py              # SQLite: expedientes y decisiones humanas
│   ├── metadata_services.py     # Crossref, PubMed y OpenAlex
│   ├── pdf_tools.py             # Lectura de PDF y citas literales
│   └── extractor.py             # Síntesis opcional con validación de citas
└── tests/                       # Pruebas automatizadas
```

La base de datos se crea en `data/revision_cientifica.db` al iniciar la aplicación. Es local, por lo que el equipo es responsable de protegerla y de aplicar su política de retención de datos.

## 7. Límites éticos y técnicos

La aplicación no evalúa la calidad metodológica completa, no reemplaza lectura crítica, no diagnostica sesgo ni determina por sí misma si una fuente es apta para publicación. La ausencia de una alerta no prueba que un estudio sea correcto, y una alerta no obliga a rechazarlo. Revise el aviso editorial, los métodos, el análisis, el contexto y las políticas de Doma antes de decidir.

Si el PDF es una imagen escaneada, el MVP marcará que no encontró texto extraíble. Para una versión posterior se puede incorporar OCR antes de ejecutar el extractor; no se debe inferir el contenido de un documento que la herramienta no pudo leer.

## 8. Solución de problemas

| Problema | Acción recomendada |
|---|---|
| `streamlit` no se reconoce | Active el entorno virtual y ejecute `pip install -r requirements.txt`. |
| No aparece síntesis por IA | Revise que `.env` exista, que `LLM_API_KEY` no esté vacía y que el endpoint/modelo correspondan a su proveedor. Aun así se mostrarán citas locales. |
| El DOI no se localiza | Confirme que el DOI no tenga un carácter adicional, pruebe el DOI en el sitio del editor y use el PDF con un identificador legible. |
| El PDF no genera texto | Compruebe que no sea un escaneo ni esté protegido; aplique OCR y vuelva a subirlo. |
| No se puede abrir una fuente pública | Compruebe conexión a Internet; el análisis local del PDF no depende de que la consulta externa sea exitosa. |

## Referencias

[1] [Crossref. *Retraction Watch*](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/)

[2] [National Center for Biotechnology Information. *APIs — Entrez Programming Utilities*](https://www.ncbi.nlm.nih.gov/home/develop/api/)

[3] [OpenAlex. *API documentation*](https://docs.openalex.org/)
