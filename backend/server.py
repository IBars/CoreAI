from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime
import httpx
import asyncio
from emergentintegrations.llm.chat import LlmChat, UserMessage


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI(title="Akıllı Arama Chatbot", description="Google arama özellikli ChatGPT tarzı chatbot")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# LLM and Search configuration
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')
GOOGLE_CSE_API_KEY = os.environ.get('GOOGLE_CSE_API_KEY')
GOOGLE_CSE_ENGINE_ID = os.environ.get('GOOGLE_CSE_ENGINE_ID')

# Response Models
class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    role: str  # "user" or "assistant"
    content: str
    sources: Optional[List[Dict[str, str]]] = None
    used_search: bool = False
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    message: ChatMessage
    success: bool
    error: Optional[str] = None

class SearchResult(BaseModel):
    title: str
    link: str
    snippet: str
    display_link: str

# Search Service
class GoogleSearchService:
    def __init__(self, api_key: str, engine_id: str):
        self.api_key = api_key
        self.engine_id = engine_id
        self.base_url = "https://www.googleapis.com/customsearch/v1"
    
    async def search(self, query: str, num_results: int = 5) -> List[SearchResult]:
        """Google Custom Search API ile arama yapar"""
        try:
            params = {
                'key': self.api_key,
                'cx': self.engine_id,
                'q': query,
                'num': min(num_results, 10),
                'lr': 'lang_tr',  # Türkçe sonuçları öncelendir
                'hl': 'tr'  # Arayüz dili Türkçe
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(self.base_url, params=params)
                response.raise_for_status()
                
                data = response.json()
                results = []
                
                if 'items' in data:
                    for item in data['items']:
                        result = SearchResult(
                            title=item.get('title', ''),
                            link=item.get('link', ''),
                            snippet=item.get('snippet', ''),
                            display_link=item.get('displayLink', '')
                        )
                        results.append(result)
                
                return results
                
        except httpx.HTTPError as e:
            logging.error(f"Google Search API hatası: {e}")
            return []
        except Exception as e:
            logging.error(f"Arama hatası: {e}")
            return []

# Chat Service
class ChatService:
    def __init__(self, llm_key: str, search_service: GoogleSearchService):
        self.llm_key = llm_key
        self.search_service = search_service
        
    def should_search(self, message: str) -> bool:
        """Mesajın web araması gerektirip gerektirmediğini belirler"""
        search_indicators = [
            'güncel', 'son', 'yeni', 'bugün', 'dün', 'bu hafta', 'bu ay',
            'haber', 'fiyat', 'kurs', 'borsa', 'hava durumu', 'trafik',
            'oranlar', 'seçim', 'maç', 'skor', 'sonuç', 'zamanı',
            'ne zaman', 'kaçta', 'nerede', 'hangi', 'kim kazandı',
            'çıktı mı', 'yayınlandı mı', 'açıklandı mı'
        ]
        
        message_lower = message.lower()
        return any(indicator in message_lower for indicator in search_indicators)
    
    async def get_chat_response(self, session_id: str, user_message: str) -> ChatMessage:
        """Kullanıcı mesajına yanıt üretir, gerekirse web araması yapar"""
        try:
            # Önce arama gerekip gerekmediğini kontrol et
            needs_search = self.should_search(user_message)
            search_results = []
            sources = []
            
            if needs_search:
                # Google'da ara
                search_results = await self.search_service.search(user_message, num_results=3)
                if search_results:
                    sources = [
                        {
                            "title": result.title,
                            "url": result.link,
                            "snippet": result.snippet
                        }
                        for result in search_results
                    ]
            
            # LLM'den yanıt al
            system_message = self._create_system_message(search_results)
            chat = LlmChat(
                api_key=self.llm_key,
                session_id=session_id,
                system_message=system_message
            ).with_model("openai", "gpt-4o-mini")
            
            # Kullanıcı mesajını gönder
            user_msg = UserMessage(text=user_message)
            response = await chat.send_message(user_msg)
            
            # Yanıt mesajını oluştur
            chat_message = ChatMessage(
                session_id=session_id,
                role="assistant",
                content=response,
                sources=sources if sources else None,
                used_search=needs_search and len(search_results) > 0
            )
            
            # MongoDB'ye kaydet
            await db.chat_messages.insert_one(chat_message.dict())
            
            return chat_message
            
        except Exception as e:
            logging.error(f"Chat yanıt hatası: {e}")
            raise HTTPException(status_code=500, detail=f"Yanıt oluşturulurken hata: {str(e)}")
    
    def _create_system_message(self, search_results: List[SearchResult]) -> str:
        """Sistem mesajını oluşturur"""
        base_message = """Sen akıllı bir Türkçe chatbot'sun. Kullanıcılara yardımcı ol, sorularını samimi ve anlaşılır bir şekilde yanıtla. 

KURALLAR:
1. Her zaman Türkçe yanıt ver
2. Kibar, samimi ve yardımsever ol
3. Gerekirse örnekler ver veya adım adım açıkla
4. Emin olmadığın bilgiler için belirt
5. Kaynak varsa mutlaka belirt"""

        if search_results:
            search_info = "\n\nGÜNCEL BİLGİLER (Google araması):\n"
            for i, result in enumerate(search_results, 1):
                search_info += f"{i}. {result.title}\n"
                search_info += f"   Kaynak: {result.display_link}\n"
                search_info += f"   Özet: {result.snippet}\n\n"
            
            search_info += "Bu güncel bilgileri kullanarak yanıt ver ve kaynaklarını belirt."
            base_message += search_info
        
        return base_message

# Initialize services
search_service = GoogleSearchService(GOOGLE_CSE_API_KEY, GOOGLE_CSE_ENGINE_ID)
chat_service = ChatService(EMERGENT_LLM_KEY, search_service)

# API Endpoints
@api_router.get("/")
async def root():
    return {"message": "Akıllı Arama Chatbot API"}

@api_router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """Chat mesajı gönder ve yanıt al"""
    try:
        # Kullanıcı mesajını kaydet
        user_message = ChatMessage(
            session_id=request.session_id,
            role="user",
            content=request.message,
            used_search=False
        )
        await db.chat_messages.insert_one(user_message.dict())
        
        # Bot yanıtını al
        bot_response = await chat_service.get_chat_response(
            request.session_id, 
            request.message
        )
        
        return ChatResponse(
            message=bot_response,
            success=True
        )
        
    except Exception as e:
        logging.error(f"Chat endpoint hatası: {e}")
        return ChatResponse(
            message=ChatMessage(
                session_id=request.session_id,
                role="assistant",
                content="Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.",
                used_search=False
            ),
            success=False,
            error=str(e)
        )

@api_router.get("/chat/{session_id}/history", response_model=List[ChatMessage])
async def get_chat_history(session_id: str, limit: int = 50):
    """Belirli bir session'ın chat geçmişini getir"""
    try:
        messages = await db.chat_messages.find(
            {"session_id": session_id}
        ).sort("timestamp", 1).limit(limit).to_list(limit)
        
        return [ChatMessage(**msg) for msg in messages]
    except Exception as e:
        logging.error(f"Chat geçmişi hatası: {e}")
        raise HTTPException(status_code=500, detail="Chat geçmişi alınamadı")

@api_router.delete("/chat/{session_id}")
async def clear_chat_history(session_id: str):
    """Belirli bir session'ın chat geçmişini temizle"""
    try:
        result = await db.chat_messages.delete_many({"session_id": session_id})
        return {"deleted_count": result.deleted_count}
    except Exception as e:
        logging.error(f"Chat geçmişi silme hatası: {e}")
        raise HTTPException(status_code=500, detail="Chat geçmişi silinemedi")

@api_router.get("/test-search")
async def test_search(q: str = "test"):
    """Google arama testi"""
    try:
        results = await search_service.search(q, num_results=3)
        return {"query": q, "results": results, "count": len(results)}
    except Exception as e:
        logging.error(f"Arama testi hatası: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()