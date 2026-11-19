import os
import sys
import json
import logging
import asyncio
from datetime import datetime
from aiohttp import web, WSMsgType
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy import Column, Integer, String, BigInteger, ForeignKey, Text, DateTime, Boolean, select, or_

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
PORT = 3000

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

if not BOT_TOKEN or not WEBAPP_URL:
    logging.error("ОШИБКА: Не заданы BOT_TOKEN или WEBAPP_URL в переменных окружения!")
    sys.exit(1)

if not WEBAPP_URL.endswith('/'):
    WEBAPP_URL += '/'

# Создаем папку для загрузки медиа, если её нет
os.makedirs('./uploads', exist_ok=True)

Base = declarative_base()
engine = create_async_engine("sqlite+aiosqlite:///app.db", echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False)
    is_admin = Column(Boolean, default=False)
    characters = relationship("Character", back_populates="owner", lazy="joined")

class Character(Base):
    __tablename__ = 'characters'
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'))
    name = Column(String, nullable=False)
    owner = relationship("User", back_populates="characters")

class Post(Base):
    __tablename__ = 'posts'
    id = Column(Integer, primary_key=True)
    author_id = Column(Integer, ForeignKey('characters.id'))
    text = Column(Text, nullable=True)
    media_url = Column(String, nullable=True)
    media_type = Column(String, nullable=True) # 'image' or 'video'
    created_at = Column(DateTime, default=datetime.utcnow)
    author = relationship("Character", lazy="joined")

class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    messages = relationship("Message", back_populates="chat")

class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer, ForeignKey('chats.id'))
    author_id = Column(Integer, ForeignKey('characters.id'))
    text = Column(Text, nullable=True)
    media_url = Column(String, nullable=True)
    media_type = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    author = relationship("Character", lazy="joined")
    chat = relationship("Chat", back_populates="messages")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.tg_id == message.from_user.id))
        user = result.scalars().first()
        if not user:
            # Создаем пользователя
            user = User(tg_id=message.from_user.id, is_admin=True)
            session.add(user)
            await session.commit()
            
            # Выдаем персонажа по умолчанию при первой регистрации
            char = Character(owner_id=user.id, name=message.from_user.first_name)
            session.add(char)
            await session.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть Приложение", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(f"Ваш ID: {message.from_user.id}\nОткройте приложение:", reply_markup=markup)

@dp.message(Command("add_char"))
async def add_char_cmd(message: types.Message):
    # Команда: /add_char <TG_ID> <Имя персонажа>
    try:
        parts = message.text.split(" ", 2)
        tg_id = int(parts[1])
        char_name = parts[2]
        
        async with async_session() as session:
            res = await session.execute(select(User).where(User.tg_id == tg_id))
            target_user = res.scalars().first()
            if not target_user:
                await message.answer("Пользователь не найден в БД. Пусть нажмет /start")
                return
            
            new_char = Character(owner_id=target_user.id, name=char_name)
            session.add(new_char)
            await session.commit()
            await message.answer(f"Персонаж '{char_name}' успешно выдан пользователю {tg_id}!")
    except Exception:
        await message.answer("Ошибка формата. Используйте: /add_char <TG_ID> <Имя>")

async def handle_upload(request):
    reader = await request.multipart()
    field = await reader.next()
    
    if not field:
        return web.json_response({'error': 'No file'}, status=400)
    
    filename = field.filename
    ext = os.path.splitext(filename)[1].lower()
    
    # Генерируем безопасное уникальное имя файла
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}"
    filepath = os.path.join('./uploads', safe_name)
    
    with open(filepath, 'wb') as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
            
    media_type = 'video' if ext in ['.mp4', '.mov', '.webm'] else 'image'
    return web.json_response({'url': f'/uploads/{safe_name}', 'type': media_type})

active_websockets = []

async def broadcast_ws(message_data):
    for ws in active_websockets:
        try:
            await ws.send_json(message_data)
        except Exception:
            pass

async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    active_websockets.append(ws)
    
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get('action')
                
                async with async_session() as session:
                    if action == 'new_post':
                        post = Post(
                            author_id=data['author_id'],
                            text=data.get('text', ''),
                            media_url=data.get('media_url'),
                            media_type=data.get('media_type')
                        )
                        session.add(post)
                        await session.commit()
                        await session.refresh(post)
                        
                        await broadcast_ws({
                            'type': 'new_post',
                            'post': {
                                'id': post.id,
                                'text': post.text,
                                'media_url': post.media_url,
                                'media_type': post.media_type,
                                'author_name': post.author.name
                            }
                        })
                        
                    elif action == 'new_message':
                        msg_obj = Message(
                            chat_id=data['chat_id'],
                            author_id=data['author_id'],
                            text=data.get('text', ''),
                            media_url=data.get('media_url'),
                            media_type=data.get('media_type')
                        )
                        session.add(msg_obj)
                        await session.commit()
                        await session.refresh(msg_obj)
                        
                        await broadcast_ws({
                            'type': 'new_message',
                            'message': {
                                'id': msg_obj.id,
                                'chat_id': msg_obj.chat_id,
                                'text': msg_obj.text,
                                'media_url': msg_obj.media_url,
                                'media_type': msg_obj.media_type,
                                'author_name': msg_obj.author.name
                            }
                        })
    finally:
        active_websockets.remove(ws)
    return ws

async def handle_init(request):
    tg_id = request.rel_url.query.get('tg_id')
    if not tg_id:
        return web.json_response({'error': 'No tg_id'})
        
    async with async_session() as session:
        res = await session.execute(select(User).where(User.tg_id == int(tg_id)))
        user = res.scalars().first()
        if not user:
            return web.json_response({'error': 'User not found'})
            
        chars = [{'id': c.id, 'name': c.name} for c in user.characters]
        
        # Получаем ленту (последние 50 постов)
        posts_res = await session.execute(select(Post).order_by(Post.created_at.desc()).limit(50))
        posts = [{
            'id': p.id, 'text': p.text, 'media_url': p.media_url, 
            'media_type': p.media_type, 'author_name': p.author.name
        } for p in posts_res.scalars().all()]
        
        # Получаем чаты
        chats_res = await session.execute(select(Chat))
        chats = []
        for c in chats_res.scalars().all():
            msg_res = await session.execute(select(Message).where(Message.chat_id == c.id).order_by(Message.created_at.asc()))
            msgs = [{
                'id': m.id, 'text': m.text, 'author_name': m.author.name,
                'media_url': m.media_url, 'media_type': m.media_type
            } for m in msg_res.scalars().all()]
            chats.append({'id': c.id, 'title': c.title, 'messages': msgs})
            
        # Создаем общий чат при первом запуске, если его нет
        if len(chats) == 0:
            general = Chat(title="Общий чат")
            session.add(general)
            await session.commit()
            chats.append({'id': general.id, 'title': general.title, 'messages': []})

        return web.json_response({
            'characters': chars,
            'posts': posts,
            'chats': chats
        })

async def create_chat(request):
    data = await request.json()
    async with async_session() as session:
        chat = Chat(title=data['title'])
        session.add(chat)
        await session.commit()
        return web.json_response({'id': chat.id, 'title': chat.title})

async def handle_index(request):
    html = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body { background: var(--tg-theme-bg-color, #1a1a1a); color: var(--tg-theme-text-color, #ffffff); overflow: hidden;}
            .tab-content { display: none; height: calc(100vh - 60px); overflow-y: auto; padding-bottom: 20px;}
            .tab-content.active { display: block; }
            .nav-btn.active { color: var(--tg-theme-button-color, #2b6cb0); border-bottom: 2px solid var(--tg-theme-button-color, #2b6cb0); }
            input, button, textarea { background: var(--tg-theme-secondary-bg-color, #2d3748); color: var(--tg-theme-text-color, #fff); }
            .btn-primary { background: var(--tg-theme-button-color, #3182ce); color: var(--tg-theme-button-text-color, #fff); }
            .post-card, .chat-msg { background: var(--tg-theme-secondary-bg-color, #2d3748); border: 1px solid var(--tg-theme-hint-color, #4a5568); }
        </style>
    </head>
    <body class="flex flex-col h-screen">
        
        <nav class="flex justify-around border-b border-gray-700 bg-black bg-opacity-20 p-2 h-[60px] shrink-0">
            <button onclick="switchTab('feed')" id="nav-feed" class="nav-btn active px-4 py-2 font-bold">Лента</button>
            <button onclick="switchTab('chats')" id="nav-chats" class="nav-btn px-4 py-2 font-bold">Чаты</button>
            <button onclick="switchTab('profile')" id="nav-profile" class="nav-btn px-4 py-2 font-bold">Персонажи</button>
        </nav>

        <div id="feed" class="tab-content active p-4">
            <div class="mb-4 p-3 rounded post-card">
                <textarea id="postText" class="w-full p-2 rounded mb-2" placeholder="Что нового у вашего персонажа?" rows="2"></textarea>
                <input type="file" id="postMedia" class="text-sm mb-2 w-full" accept="image/*,video/*">
                <button onclick="createPost()" class="btn-primary w-full p-2 rounded font-bold">Опубликовать</button>
            </div>
            <div id="feedContainer" class="space-y-4"></div>
        </div>

        <div id="chats" class="tab-content p-4">
            <div id="chatList" class="space-y-2"></div>
            <div class="mt-4 flex gap-2">
                <input type="text" id="newChatName" placeholder="Название нового чата" class="flex-1 p-2 rounded">
                <button onclick="createNewChat()" class="btn-primary p-2 rounded">+</button>
            </div>
            
            <div id="chatWindow" class="hidden fixed inset-0 bg-[var(--tg-theme-bg-color)] z-50 flex flex-col h-screen">
                <div class="p-3 border-b flex items-center bg-black bg-opacity-20">
                    <button onclick="closeChat()" class="mr-4 text-2xl">←</button>
                    <h2 id="chatTitle" class="font-bold text-lg">Чат</h2>
                </div>
                <div id="chatMessages" class="flex-1 overflow-y-auto p-4 space-y-3"></div>
                <div class="p-3 border-t flex gap-2 bg-black bg-opacity-20">
                    <input type="file" id="msgMedia" class="hidden" accept="image/*,video/*">
                    <button onclick="document.getElementById('msgMedia').click()" class="text-2xl px-2">📎</button>
                    <input type="text" id="msgText" class="flex-1 p-2 rounded" placeholder="Сообщение...">
                    <button onclick="sendMessage()" class="btn-primary px-4 rounded font-bold">➔</button>
                </div>
            </div>
        </div>

        <div id="profile" class="tab-content p-4">
            <h2 class="text-xl font-bold mb-4">Выбор персонажа</h2>
            <div id="charactersList" class="space-y-2"></div>
            <p class="text-sm text-gray-400 mt-4">Чтобы добавить персонажа, админ должен отправить в бота команду: /add_char Ваш_ID Имя</p>
        </div>

        <script>
            const tg = window.Telegram.WebApp;
            tg.expand();
            
            let state = {
                tg_id: tg.initDataUnsafe?.user?.id || 1, // 1 для тестов
                characters: [],
                activeCharId: null,
                posts: [],
                chats: [],
                currentChatId: null,
                ws: null
            };

            async function initApp() {
                const res = await fetch(`/api/init?tg_id=${state.tg_id}`);
                const data = await res.json();
                if(data.error) return alert("Ошибка загрузки: " + data.error);
                
                state.characters = data.characters;
                if(state.characters.length > 0) state.activeCharId = state.characters[0].id;
                state.posts = data.posts;
                state.chats = data.chats;
                
                renderCharacters();
                renderFeed();
                renderChats();
                connectWebSocket();
            }

            function connectWebSocket() {
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                state.ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
                
                state.ws.onmessage = (event) => {
                    const data = JSON.parse(event.data);
                    if (data.type === 'new_post') {
                        state.posts.unshift(data.post);
                        if (document.getElementById('feed').classList.contains('active')) renderFeed();
                    } else if (data.type === 'new_message') {
                        const chat = state.chats.find(c => c.id === data.message.chat_id);
                        if (chat) chat.messages.push(data.message);
                        if (state.currentChatId === data.message.chat_id) renderMessages();
                    }
                };
            }

            async function uploadFile(fileInputId) {
                const file = document.getElementById(fileInputId).files[0];
                if (!file) return null;
                const fd = new FormData();
                fd.append('file', file);
                const res = await fetch('/api/upload', { method: 'POST', body: fd });
                return await res.json();
            }

            function renderCharacters() {
                const html = state.characters.map(c => `
                    <div onclick="selectChar(${c.id})" class="p-3 rounded border ${c.id === state.activeCharId ? 'border-blue-500 bg-blue-900 bg-opacity-30' : 'border-gray-600'} cursor-pointer flex justify-between items-center">
                        <span class="font-bold">${c.name}</span>
                        ${c.id === state.activeCharId ? '<span>✅</span>' : ''}
                    </div>
                `).join('');
                document.getElementById('charactersList').innerHTML = html;
            }

            function selectChar(id) {
                state.activeCharId = id;
                renderCharacters();
                try { tg.HapticFeedback.selectionChanged(); } catch(e) {}
            }

            function renderFeed() {
                const html = state.posts.map(p => `
                    <div class="post-card p-4 rounded shadow">
                        <div class="font-bold mb-2 text-blue-400">${p.author_name}</div>
                        ${p.media_url ? (p.media_type === 'video' ? 
                            `<video src="${p.media_url}" controls class="w-full rounded mb-2"></video>` : 
                            `<img src="${p.media_url}" class="w-full rounded mb-2">`) : ''}
                        <div>${p.text || ''}</div>
                    </div>
                `).join('');
                document.getElementById('feedContainer').innerHTML = html;
            }

            function renderChats() {
                const html = state.chats.map(c => `
                    <div onclick="openChat(${c.id})" class="p-4 rounded border border-gray-600 cursor-pointer flex justify-between items-center">
                        <span class="font-bold">${c.title}</span>
                        <span class="text-gray-400 text-sm">${c.messages.length} сообщ.</span>
                    </div>
                `).join('');
                document.getElementById('chatList').innerHTML = html;
            }

            async function createPost() {
                if (!state.activeCharId) return alert("Сначала выберите персонажа во вкладке 'Персонажи'!");
                const text = document.getElementById('postText').value;
                const mediaRes = await uploadFile('postMedia');
                
                if(!text && !mediaRes) return;
                
                state.ws.send(JSON.stringify({
                    action: 'new_post',
                    author_id: state.activeCharId,
                    text: text,
                    media_url: mediaRes?.url,
                    media_type: mediaRes?.type
                }));
                
                document.getElementById('postText').value = '';
                document.getElementById('postMedia').value = '';
                try { tg.HapticFeedback.impactOccurred('light'); } catch(e) {}
            }

            async function sendMessage() {
                if (!state.activeCharId) return alert("Выберите персонажа!");
                const text = document.getElementById('msgText').value;
                const mediaRes = await uploadFile('msgMedia');
                
                if(!text && !mediaRes) return;
                
                state.ws.send(JSON.stringify({
                    action: 'new_message',
                    chat_id: state.currentChatId,
                    author_id: state.activeCharId,
                    text: text,
                    media_url: mediaRes?.url,
                    media_type: mediaRes?.type
                }));
                
                document.getElementById('msgText').value = '';
                document.getElementById('msgMedia').value = '';
            }

            async function createNewChat() {
                const title = document.getElementById('newChatName').value;
                if(!title) return;
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title})
                });
                const chat = await res.json();
                chat.messages = [];
                state.chats.push(chat);
                renderChats();
                document.getElementById('newChatName').value = '';
            }

            function switchTab(tabId) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                document.getElementById('nav-' + tabId).classList.add('active');
                try { tg.HapticFeedback.selectionChanged(); } catch(e) {}
            }

            function openChat(id) {
                state.currentChatId = id;
                const chat = state.chats.find(c => c.id === id);
                document.getElementById('chatTitle').innerText = chat.title;
                document.getElementById('chatWindow').classList.remove('hidden');
                renderMessages();
            }

            function closeChat() {
                document.getElementById('chatWindow').classList.add('hidden');
                state.currentChatId = null;
            }

            function renderMessages() {
                const chat = state.chats.find(c => c.id === state.currentChatId);
                const container = document.getElementById('chatMessages');
                
                container.innerHTML = chat.messages.map(m => `
                    <div class="chat-msg p-3 rounded-lg w-4/5 ${m.author_name === state.characters.find(c=>c.id === state.activeCharId)?.name ? 'ml-auto bg-blue-900 bg-opacity-30 border-blue-500' : ''}">
                        <div class="text-xs text-gray-400 mb-1">${m.author_name}</div>
                        ${m.media_url ? (m.media_type === 'video' ? 
                            `<video src="${m.media_url}" controls class="w-full rounded mb-1"></video>` : 
                            `<img src="${m.media_url}" class="w-full rounded mb-1">`) : ''}
                        <div>${m.text || ''}</div>
                    </div>
                `).join('');
                container.scrollTop = container.scrollHeight;
            }

            initApp();
        </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def main():
    await on_startup()
    
    app = web.Application()
    
    # API endpoints
    app.router.add_get('/ws', handle_ws)
    app.router.add_get('/api/init', handle_init)
    app.router.add_post('/api/upload', handle_upload)
    app.router.add_post('/api/chat', create_chat)
    
    # Раздача медиафайлов
    app.router.add_static('/uploads', './uploads')
    
    # HTML Фронтенд 
    app.router.add_route('*', '/{tail:.*}', handle_index)
    app.router.add_route('*', '/', handle_index)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"✅ Сервер запущен на порту {PORT}")
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("✅ Запускаем бота...")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ Ошибка бота: {e}")

if __name__ == "__main__":
    asyncio.run(main())