"""Query-only planning boundaries, calendar precision and transport fallback."""
from datetime import date, datetime, timezone
from http.client import IncompleteRead
import json
import unittest
from unittest.mock import patch

from src.settings import AISettings
from src.ai_transport import TransportError
from webapp import query_planning as planner


CAPABILITY = {'protocol':'deepseek','can_disable':True,'can_enable':True,'levels':['low','high','max']}


def period(expression,start,end,precision='range'):
    return {'expression':expression,'start':start,'end':end,'precision':precision}


def fields(*, facts=(), periods=(), queries=('检索相关税务依据',)):
    return json.dumps({'facts':list(facts),'periods':list(periods),'queries':list(queries)},ensure_ascii=False)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.settings = AISettings(enabled=True, api_key='synthetic', reasoning_effort='max',
                                   reasoning_protocol='deepseek', disable_thinking=False)

    def test_none_and_low_apply_to_this_call_only_and_preserve_original_settings(self):
        for effort in ('none','low'):
            with self.subTest(effort=effort), patch.object(planner,'chat_content',return_value=fields()) as request:
                result = planner.plan_query(self.settings,CAPABILITY,'解释税务规定',effort=effort)
            self.assertEqual(result['status'],'used')
            sent = request.call_args.args[0]
            self.assertEqual(sent.reasoning_effort,effort)
            self.assertEqual(sent.disable_thinking,effort=='none')
            self.assertLessEqual(sent.timeout,25)
            self.assertLessEqual(request.call_args.kwargs['max_tokens'],4096)
            self.assertEqual(self.settings.reasoning_effort,'max')
            self.assertFalse(self.settings.disable_thinking)

    def test_unknown_capability_or_insufficient_budget_never_calls_api(self):
        with patch.object(planner,'chat_content') as request:
            self.assertEqual(planner.plan_query(self.settings,{},'解释')['status'],'skipped')
            tiny = AISettings(enabled=True,api_key='synthetic',max_calls=1)
            self.assertEqual(planner.plan_query(tiny,CAPABILITY,'解释')['status'],'skipped')
        request.assert_not_called()

    def test_invented_facts_numeric_queries_and_extra_answer_are_rejected(self):
        bad = [fields(facts=('公司销售额一百万元',)), fields(queries=('按百分之十三或13%计算税额',)),
               json.dumps({'facts':[],'queries':['检索税务'],'periods':[],'answer':'直接免税'})]
        for content in bad:
            with self.subTest(content=content), patch.object(planner,'chat_content',return_value=content):
                result = planner.plan_query(self.settings,CAPABILITY,'用户只问了一般规定')
                self.assertEqual(result['status'],'fallback')
                self.assertEqual(result['queries'],[])

    def test_timeout_or_invalid_json_falls_back_once(self):
        for effect in (TransportError('timeout'), 'not json'):
            with self.subTest(effect=type(effect).__name__), patch.object(planner,'chat_content') as request:
                if isinstance(effect,Exception):
                    request.side_effect=effect
                else:
                    request.return_value=effect
                result=planner.plan_query(self.settings,CAPABILITY,'解释税务规定')
            self.assertEqual(result['status'],'fallback')
            self.assertTrue(result['request_attempted'])
            self.assertEqual(request.call_count,1)

    def test_connection_reset_and_truncated_http_response_fall_back_without_retry(self):
        for error in (ConnectionResetError('synthetic reset'), IncompleteRead(b'synthetic partial response')):
            with self.subTest(error=type(error).__name__), patch.object(planner,'chat_content',side_effect=error) as request:
                result=planner.plan_query(self.settings,CAPABILITY,'解释税务规定')
            self.assertEqual(result['status'],'fallback')
            self.assertEqual(result['reason'],'upstream_network')
            self.assertEqual(result['queries'],[])
            self.assertTrue(result['request_attempted'])
            self.assertEqual(request.call_count,1)
            self.assertNotIn('synthetic',json.dumps(result))

    def test_compound_quarters_and_separate_quotes_have_same_precise_periods(self):
        question='比较2025年第四季度和2026年第一季度的规定'
        expected=[{'start':'2025-10-01','end':'2025-12-31'},{'start':'2026-01-01','end':'2026-03-31'}]
        for times in (('2025年第四季度','2026年第一季度'),('2025年第四季度和2026年第一季度',)):
            periods=[period(times[0],**expected[0],precision='quarter'),
                     period(times[-1],**expected[1],precision='quarter')]
            result=planner.validate_plan(fields(periods=periods),question,(),date(2026,11,15))
            self.assertEqual(result['periods'],expected)

    def test_continuous_span_cannot_be_reduced_to_first_endpoint(self):
        question='从2025年12月至2026年2月的交易'
        for periods in ([period('2025年12月','2025-12-01','2025-12-31','month')],
                        [period('2025年12月','2025-12-01','2025-12-31','month'),
                         period('2026年2月','2026-02-01','2026-02-28','month')]):
            with self.subTest(periods=periods),self.assertRaises(ValueError):
                planner.validate_plan(fields(periods=periods),question,(),date(2026,11,15))
        result=planner.validate_plan(fields(periods=[period(question,'2025-12-01','2026-02-28')]),question,(),date(2026,11,15))
        self.assertEqual(result['periods'],[{'start':'2025-12-01','end':'2026-02-28'}])

    def test_original_relative_months_are_calculated_in_program(self):
        actual=planner._time_periods('去年最后一个月与今年头一个月',date(2026,11,15))
        self.assertEqual(actual,[{'start':'2025-12-01','end':'2025-12-31'},
                                 {'start':'2026-01-01','end':'2026-01-31'}])

    def test_planner_cannot_replace_dates_or_drop_discrete_requested_year(self):
        for question,content in [
                ('按去年查',fields()),
                ('按2025年12月与2026年2月查',fields(periods=[period('2025年12月','2025-12-01','2025-12-31','month')])),
                ('按2025年12月查',fields(periods=[period('2025年12月','2026-02-01','2026-02-28','month')]))]:
            with self.subTest(question=question),self.assertRaises(ValueError):
                planner.validate_plan(content,question,(),date(2026,11,15))

    def test_relative_year_month_and_leap_month_are_complete_iso_ranges(self):
        for expression,start,end,precision in [
                ('去年','2025-01-01','2025-12-31','year'),
                ('去年最后一个月','2025-12-01','2025-12-31','month'),
                ('今年头一个月','2026-01-01','2026-01-31','month'),
                ('2024年2月','2024-02-01','2024-02-29','month')]:
            with self.subTest(expression=expression):
                item=period(expression,start,end,precision)
                result=planner.validate_plan(fields(periods=[item]),expression+'的销售',(),date(2026,11,15))
                self.assertEqual(result['time_ranges'],[item])

    def test_unknown_natural_language_shape_does_not_need_a_second_phrase_parser(self):
        # The model returns ISO dates; the old phrase parser cannot interpret
        # every phrase. Do not reject an otherwise valid normalized interval.
        item=period('去年年末','2025-12-01','2025-12-31','month')
        result=planner.validate_plan(fields(periods=[item]),'去年年末的销售',(),date(2026,11,15))
        self.assertEqual(result['time_ranges'],[item])

    def test_bad_format_invalid_calendar_and_partial_month_or_year_are_rejected(self):
        for start,end,precision in [
                ('2025/12/01','2025-12-31','month'),('2025-02-30','2025-02-30','day'),
                ('2025-12-01','2025-12-30','month'),('2025-01-01','2025-12-30','year'),
                ('2025-10-01','2025-12-30','quarter'),('2025-12-31','2025-12-01','range')]:
            with self.subTest(start=start,end=end),self.assertRaises(ValueError):
                planner.validate_plan(fields(periods=[period('去年',start,end,precision)]),'去年',(),date(2026,11,15))

    def test_question_timestamp_is_frozen_and_converted_to_beijing_before_new_year(self):
        # UTC is still Dec 31; the question was received on Jan 1 in Beijing.
        stamp=datetime(2026,12,31,16,0,tzinfo=timezone.utc)
        content=fields(periods=[period('去年','2026-01-01','2026-12-31','year')])
        with patch.object(planner,'chat_content',return_value=content) as request:
            result=planner.plan_query(self.settings,CAPABILITY,'去年销售',reference_time=stamp)
        sent=json.loads(request.call_args.args[1][1]['content'])
        self.assertEqual(sent['reference_time'],'2027-01-01T00:00:00+08:00')
        self.assertEqual(result['reference_time'],sent['reference_time'])
        self.assertEqual(result['periods'],[{'start':'2026-01-01','end':'2026-12-31'}])

    def test_current_turn_time_replaces_previous_turn_time(self):
        item=period('今年','2026-01-01','2026-12-31','year')
        result=planner.validate_plan(fields(periods=[item]),'那今年呢？',('去年销售如何处理',),date(2026,11,15))
        self.assertEqual(result['time_ranges'],[item])

    def test_law_version_year_need_not_be_invented_as_a_business_period(self):
        result=planner.validate_plan(fields(),'请按2026年增值税法查询抵扣规定',(),date(2026,11,15))
        self.assertEqual(result['periods'],[])
