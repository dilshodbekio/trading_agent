import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from langchain_core.messages import AIMessage
from bot import ConversationAgent, create_router, split_answer


class FakeGraph:
    def __init__(self):
        self.inputs = []
    def invoke(self, state, config):
        self.inputs.append(state['messages'])
        return {'messages': [*state['messages'], AIMessage(content='Answer')]}


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sender_patch = patch('bot.ChatActionSender.typing')
        self.typing = sender_patch.start()
        self.addCleanup(sender_patch.stop)

    async def test_history_isolation_and_followup(self):
        graph = FakeGraph()
        agent = ConversationAgent(graph)
        await agent.ask((1,1), '2024?')
        await agent.ask((1,1), '2025?')
        await agent.ask((2,2), 'Hi')
        self.assertEqual([len(x) for x in graph.inputs], [1,3,1])
        await agent.reset((1,1))
        self.assertNotIn((1,1), agent.history)
        self.assertIn((2,2), agent.history)

    async def test_same_user_requests_are_serialized(self):
        graph = FakeGraph()
        agent = ConversationAgent(graph)
        await asyncio.gather(agent.ask((1,1),'first'), agent.ask((1,1),'second'))
        self.assertEqual([len(x) for x in graph.inputs], [1,3])

    async def test_failed_graph_preserves_history(self):
        graph = FakeGraph()
        agent = ConversationAgent(graph)
        await agent.ask((1,1),'hello')
        previous = list(agent.history[(1,1)])
        graph.invoke = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('failure'))
        with self.assertRaises(RuntimeError):
            await agent.ask((1,1),'question')
        self.assertEqual(agent.history[(1,1)], previous)

    async def test_message_handler_sends_answer(self):
        agent = SimpleNamespace(ask=AsyncMock(return_value='106647'), reset=AsyncMock())
        router = create_router(agent)
        handler = next(h.callback for h in router.message.handlers if h.callback.__name__=='answer')
        message = SimpleNamespace(bot=object(), message_thread_id=None, chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=20),
                                  text='Count 2024', answer=AsyncMock())
        await handler(message)
        agent.ask.assert_awaited_once_with((10,20),'Count 2024')
        self.typing.assert_called_once_with(bot=message.bot, chat_id=10, message_thread_id=None)
        self.typing.return_value.__aenter__.assert_awaited_once()
        self.typing.return_value.__aexit__.assert_awaited_once()
        message.answer.assert_awaited_once_with('106647',parse_mode=None)

    async def test_error_handler_does_not_expose_exception(self):
        agent = SimpleNamespace(ask=AsyncMock(side_effect=RuntimeError('private detail')),reset=AsyncMock())
        router = create_router(agent)
        handler = next(h.callback for h in router.message.handlers if h.callback.__name__=='answer')
        message = SimpleNamespace(bot=object(), message_thread_id=None, chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=20),
                                  text='Count', answer=AsyncMock())
        with self.assertLogs('bot',level='ERROR'):
            await handler(message)
        self.assertNotIn('private detail',message.answer.call_args.args[0])

    async def test_long_unicode_answer_split(self):
        text = 'Hello 😀' * 2000
        chunks = list(split_answer(text))
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(c.encode('utf-16-le'))//2 <=4000 for c in chunks))


if __name__=='__main__':
    unittest.main()

