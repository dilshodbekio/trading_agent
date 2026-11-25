import json
import unittest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from graph import create_graph


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def bind_tools(self, tools):
        self.tool_names = [tool.name for tool in tools]
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return next(self.responses)


class GraphTests(unittest.TestCase):
    def test_direct_answer_ends_without_tools(self):
        model = ScriptedModel([AIMessage(content='Hello!')])
        result = create_graph(model).invoke({'messages':[HumanMessage('Hi')]})
        self.assertEqual([m.type for m in result['messages']], ['human','ai'])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(set(model.tool_names), {'count_deals','get_trade_value','get_deal','search_products','get_product_stats'})

    def test_tool_returns_to_supervisor(self):
        model = ScriptedModel([
            AIMessage(content='', tool_calls=[{'name':'count_deals','args':{},'id':'count1','type':'tool_call'}]),
            AIMessage(content='500,000 registered trades, including all statuses.'),
        ])
        result = create_graph(model).invoke({'messages':[HumanMessage('How many trades?')]})
        self.assertEqual([m.type for m in result['messages']], ['human','ai','tool','ai'])
        tool_message = result['messages'][2]
        self.assertEqual(json.loads(tool_message.content)['count'], 500000)
        self.assertEqual(tool_message.tool_call_id, 'count1')
        self.assertIsInstance(model.calls[1][-1], ToolMessage)

    def test_multiple_tool_rounds(self):
        model = ScriptedModel([
            AIMessage(content='',tool_calls=[{'name':'get_deal','args':{'deal_id':100001},'id':'a','type':'tool_call'}]),
            AIMessage(content='',tool_calls=[{'name':'count_deals','args':{'start_date':'2024-01-01','end_date':'2024-12-31'},'id':'b','type':'tool_call'}]),
            AIMessage(content='Done'),
        ])
        result = create_graph(model).invoke({'messages':[HumanMessage('Look up a trade and count 2024 trades.')]})
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(len(result['messages']), 6)
        self.assertEqual(json.loads(result['messages'][4].content)['count'], 106647)

    def test_tool_error_returns_to_supervisor(self):
        model = ScriptedModel([
            AIMessage(content='',tool_calls=[{'name':'count_deals','args':{'start_date':'bad'},'id':'bad1','type':'tool_call'}]),
            AIMessage(content='Please provide a valid date.'),
        ])
        result = create_graph(model).invoke({'messages':[HumanMessage('Count trades.')]})
        self.assertIn('YYYY-MM-DD', result['messages'][2].content)
        self.assertEqual(result['messages'][-1].content, 'Please provide a valid date.')


if __name__ == '__main__':
    unittest.main()
