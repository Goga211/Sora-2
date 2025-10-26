import asyncpg
import os
from dotenv import load_dotenv
from typing import Optional, Dict, Any

load_dotenv()

class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """Подключение к базе данных PostgreSQL"""
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL не найден в переменных окружения")
        
        self.pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        
        # Создаем таблицы если их нет
        await self.create_tables()
    
    async def close(self):
        """Закрытие соединения с базой данных"""
        if self.pool:
            await self.pool.close()
    
    async def create_tables(self):
        """Создание таблиц в базе данных"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    generations_left INTEGER DEFAULT 0
                )
            """)
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Получение пользователя по ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", user_id
            )
            return dict(row) if row else None
    
    async def create_user(self, user_id: int) -> Dict[str, Any]:
        """Создание нового пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO users (user_id)
                VALUES ($1)
                RETURNING *
            """, user_id)
            return dict(row)
    
    async def update_user_generations(self, user_id: int, generations_left: int):
        """Обновление количества генераций пользователя"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET generations_left = $1 WHERE user_id = $2",
                generations_left, user_id
            )
    
    async def add_generations(self, user_id: int, amount: int):
        """Добавление генераций к балансу пользователя"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET generations_left = generations_left + $1 WHERE user_id = $2",
                amount, user_id
            )
    
    async def use_generation(self, user_id: int) -> bool:
        """Использование одной генерации. Возвращает True если успешно, False если недостаточно генераций"""
        async with self.pool.acquire() as conn:
            # Проверяем есть ли генерации
            user = await conn.fetchrow(
                "SELECT generations_left FROM users WHERE user_id = $1", user_id
            )
            
            if not user or user['generations_left'] <= 0:
                return False
            
            # Уменьшаем количество генераций
            await conn.execute(
                "UPDATE users SET generations_left = generations_left - 1 WHERE user_id = $1",
                user_id
            )
            return True
    
    
    async def has_generations(self, user_id: int) -> bool:
        """Проверка есть ли у пользователя доступные генерации"""
        async with self.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT generations_left FROM users WHERE user_id = $1", user_id
            )
            return user and user['generations_left'] > 0

# Глобальный экземпляр базы данных
db = Database()
