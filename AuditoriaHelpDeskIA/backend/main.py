# backend/main.py
import sqlite3
import sys 
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- Monitorización y Logs ---
from prometheus_fastapi_instrumentator import Instrumentator
from loguru import logger

# LangChain
from langchain_core.prompts import PromptTemplate
from langchain_ollama.llms import OllamaLLM
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain.chains import RetrievalQA

# --- 1. CONFIGURACIÓN DE LOGGING ---
logger.remove()
logger.add(sys.stdout, serialize=True, enqueue=True)

# Interceptar logs de Uvicorn para que salgan con formato Loguru
class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.log(level, record.getMessage())

logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

# --- 2. CONFIGURACIÓN GLOBAL ---
VECTOR_STORE_DIR = "vector_store"
DB_PATH = "tickets.db"
app = FastAPI(title="Corporate EPIS Pilot - Audit Exam")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# Instrumentación Prometheus
Instrumentator().instrument(app).expose(app)

# --- 3. CONFIGURACIÓN IA (SMOLLM:360M) ---
# Usamos temperature 0 para máxima precisión lógica
llm = OllamaLLM(
    model="smollm:360m", 
    temperature=0, 
    base_url="http://host.docker.internal:11434"
)

embeddings = HuggingFaceEmbeddings(model_name="intfloat/multilingual-e5-large")
vector_store = Chroma(persist_directory=VECTOR_STORE_DIR, embedding_function=embeddings)
retriever = vector_store.as_retriever()

# --- 4. LÓGICA DE NEGOCIO ---

def create_support_ticket(description: str) -> str:
    """Inserta el ticket en SQLite"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        clean_desc = description.replace("ACTION_CREATE_TICKET:", "").strip()
        if not clean_desc: clean_desc = "Problema reportado sin detalles"
        
        cursor.execute("INSERT INTO tickets (description, status) VALUES (?, ?)", (clean_desc, "Abierto"))
        tid = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info(f"Ticket creado ID: {tid}")
        return f"He registrado tu ticket #{tid} con el detalle: '{clean_desc}'. Un humano lo revisará."
    except Exception as e:
        logger.error(f"Error DB: {e}")
        return "Error al guardar ticket."

# --- 5. ROUTER HÍBRIDO (Optimizado para modelos pequeños) ---
# smollm falla con JSON output parsers complejos. Usamos detección directa.

def simple_router(text: str) -> str:
    text = text.lower()
    # Palabras clave de despedida
    if any(w in text for w in ["adios", "chau", "gracias", "hasta luego", "bye"]):
        return "despedida"
    # Palabras clave de problemas técnicos
    if any(w in text for w in ["falla", "error", "no funciona", "roto", "problema", "ayuda", "ticket", "malo"]):
        return "reporte_de_problema"
    # Por defecto
    return "pregunta_general"

# Chain de RAG simple
rag_template = "Contexto: {context}\nPregunta: {question}\nRespuesta breve en español:"
rag_prompt = PromptTemplate.from_template(rag_template)
rag_chain = RetrievalQA.from_chain_type(llm=llm, chain_type="stuff", retriever=retriever, chain_type_kwargs={"prompt": rag_prompt})

# --- 6. ENDPOINT ---
@app.get("/ask")
def ask_question(question: str):
    logger.info(f"Pregunta recibida: {question}")
    
    # CASO A: El frontend manda explícitamente la acción
    if "ACTION_CREATE_TICKET" in question:
        return {"answer": create_support_ticket(question), "follow_up_required": False}

    # CASO B: Flujo normal
    try:
        intent = simple_router(question)
        logger.info(f"Intención: {intent}")

        answer = ""
        follow_up = False

        if intent == "despedida":
            answer = "¡Hasta luego! Espero haberte ayudado."
        
        elif intent == "pregunta_general":
            # Intenta responder con RAG
            res = rag_chain.invoke({"query": question})
            answer = res.get("result", "No encontré información.")
        
        elif intent == "reporte_de_problema":
            # Intenta dar solución primero
            res = rag_chain.invoke({"query": question})
            solucion = res.get("result", "No tengo una solución exacta en mis manuales.")
            
            # Prompt para forzar el feedback
            answer = f"{solucion}\n\n¿Esta información resolvió tu problema? (Si no, di 'No' para crear ticket)"
            follow_up = True 

        return {"answer": answer, "follow_up_required": follow_up}

    except Exception as e:
        logger.error(f"Error critico: {e}")
        return {"answer": "Error interno del sistema.", "follow_up_required": False}