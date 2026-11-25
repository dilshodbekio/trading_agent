"""Telegram interface. Run python bot.py; stop with Ctrl+C."""
import asyncio
import logging
import os
from collections import defaultdict
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from graph import create_graph

logger = logging.getLogger(__name__)


class ConversationAgent:
    """Separate memory by chat/user; serialize messages in each conversation."""
    def __init__(self, graph):
        self.graph = graph
        self.history = {}
        self.locks = defaultdict(asyncio.Lock)

    async def ask(self, key, text):
        async with self.locks[key]:
            messages = [*self.history.get(key, []), HumanMessage(text)]
            # The graph uses synchronous Gemini calls, so run it off the event loop.
            result = await asyncio.to_thread(self.graph.invoke, {'messages': messages},
                                            config={'recursion_limit': 20})
            answer = result['messages'][-1].text
            if not answer.strip():
                raise ValueError('Model returned no text answer')
            self.history[key] = result['messages']
            return answer

    async def reset(self, key):
        async with self.locks[key]:
            self.history.pop(key, None)


def split_answer(text, limit=4000):
    # Conservative UTF-16 budget also handles emoji safely under Telegram's limit.
    chunk, size = [], 0
    for character in text:
        units = len(character.encode('utf-16-le')) // 2
        if size + units > limit:
            yield ''.join(chunk)
            chunk, size = [], 0
        chunk.append(character)
        size += units
    if chunk:
        yield ''.join(chunk)


def create_router(agent):
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message):
        await message.answer('Hello! I am the UZEX Trading Agent. Ask about trade counts, total value in UZS, or a deal ID.\nExample: How many trades happened in 2024?\n/reset clears your conversation. Memory clears when the bot restarts.')

    @router.message(Command('reset'))
    async def reset(message: Message):
        if message.from_user:
            await agent.reset((message.chat.id, message.from_user.id))
        await message.answer('Conversation cleared. / Suhbat xotirasi tozalandi.')

    @router.message(F.text)
    async def answer(message: Message):
        if not message.from_user or not message.text:
            return
        if message.text.startswith('/'):
            await message.answer('Available commands: /start, /reset')
            return
        key = (message.chat.id, message.from_user.id)
        try:
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id,
                                               message_thread_id=message.message_thread_id):
                response = await agent.ask(key, message.text)
        except Exception as error:
            # Do not log provider exception text: it can contain credentials or user data.
            logger.error('Graph request failed (%s)', type(error).__name__)
            await message.answer('Could not complete that request. Please try again. / So‘rov bajarilmadi. Qayta urinib ko‘ring.')
            return
        for chunk in split_answer(response):
            await message.answer(chunk, parse_mode=None)

    @router.message()
    async def unsupported(message: Message):
        await message.answer('Please send a text question. / Savolingizni matn shaklida yuboring.')

    return router


async def main():
    load_dotenv(Path(__file__).resolve().parent / '.env')
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError('Add TELEGRAM_BOT_TOKEN to your .env file.')
    agent = ConversationAgent(create_graph())
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(agent))
    async with Bot(token=token) as bot:
        print('UZEX Telegram bot is starting. Stop with Ctrl+C.')
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())

