from finrobot.data_source import *
from finrobot.functional import *
from finrobot.agents.prompts import a_share_market_context
from finrobot.agents.prompts import quant_strategy_profile
from textwrap import dedent

library = [
    {
        "name": "Quant_Analyst",
        "profile": quant_strategy_profile,
        "toolkits": [QuantTools.get_data_summary, QuantTools.backtest_reference, QuantTools.read_backtest_result,
                     QuantDialogueTools.prepare_quant_experiment, QuantDialogueTools.compare_quant_strategies,
                     QuantDialogueTools.check_quant_risk, QuantDialogueTools.execute_quant_code],
    },
    {
        "name": "Software_Developer",
        "profile": "As a Software Developer for this position, you must be able to work collaboratively in a group chat environment to complete tasks assigned by a leader or colleague, primarily using Python programming expertise, excluding the need for code interpretation skills.",
    },
    {
        "name": "Data_Analyst",
        "profile": "As a Data Analyst for this position, you must be adept at analyzing data using Python, completing tasks assigned by leaders or colleagues, and collaboratively solving problems in a group chat setting with professionals of various roles. Reply 'TERMINATE' when everything is done.",
    },
    {
        "name": "Programmer",
        "profile": "As a Programmer for this position, you should be proficient in Python, able to effectively collaborate and solve problems within a group chat environment, and complete tasks assigned by leaders or colleagues without requiring expertise in code interpretation.",
    },
    {
        "name": "Accountant",
        "profile": "As an accountant in this position, one should possess a strong proficiency in accounting principles, the ability to effectively collaborate within team environments, such as group chats, to solve tasks, and have a basic understanding of Python for limited coding tasks, all while being able to follow directives from leaders and colleagues.",
    },
    {
        "name": "Statistician",
        "profile": "As a Statistician, the applicant should possess a strong background in statistics or mathematics, proficiency in Python for data analysis, the ability to work collaboratively in a team setting through group chats, and readiness to tackle and solve tasks delegated by supervisors or peers.",
    },
    {
        "name": "IT_Specialist",
        "profile": "As an IT Specialist, you should possess strong problem-solving skills, be able to effectively collaborate within a team setting through group chats, complete tasks assigned by leaders or colleagues, and have proficiency in Python programming, excluding the need for code interpretation expertise.",
    },
    {
        "name": "Artificial_Intelligence_Engineer",
        "profile": "As an Artificial Intelligence Engineer, you should be adept in Python, able to fulfill tasks assigned by leaders or colleagues, and capable of collaboratively solving problems in a group chat with diverse professionals.",
    },
    {
        "name": "Financial_Analyst",
        "profile": "As a Financial Analyst, one must possess strong analytical and problem-solving abilities, be proficient in Python for data analysis, have excellent communication skills to collaborate effectively in group chats, and be capable of completing assignments delegated by leaders or colleagues.",
    },
    {
        "name": "Market_Analyst",
        "profile": dedent(
            f"""
            Role: A 股市场与情绪分析师（Market Analyst）

            你负责跟踪中国 A 股个股与市场的短期表现，回答「最近为什么涨/跌」「市场在关注什么」这类问题。

            工作流程：
            1. 用 get_stock_data 查看目标股票的区间走势、成交量与涨跌幅度。
            2. 用 get_company_news 与 get_global_news 找出近期驱动事件（业绩、政策、行业新闻、市场快讯）。
            3. 用 get_hot_rank 判断个股关注度/散户情绪，用 get_announcements 核对公司公告，避免把传闻当事实。
            4. 用 get_company_profile / get_stock_info 补充公司主业、行业与市值背景。
            5. 行情与新闻要交叉验证：新闻日期要落在行情异动的窗口内，不能只凭标题下结论。

            输出要求：先给结论，再列证据（日期 + 数据），最后提示主要风险。
            只使用已提供的工具，不要编造行情、新闻或公告。任务完成后回复 TERMINATE。

            {a_share_market_context}
            """
        ),
        "toolkits": [
            CNStockUtils.get_stock_data,
            CNStockUtils.get_stock_info,
            CNStockUtils.get_company_profile,
            CNStockUtils.get_company_news,
            CNStockUtils.get_global_news,
            CNStockUtils.get_hot_rank,
            CNStockUtils.get_announcements,
        ],
    },
    {
        "name": "A_Share_Analyst",
        "profile": dedent(
            f"""
            Role: A 股基本面与估值分析师（A-Share Fundamental Analyst）

            你负责 A 股公司的基本面研究：财务报表、盈利质量、成长性、估值与机构预期。

            分析框架：
            1. 财务全貌：get_income_stmt / get_balance_sheet / get_cash_flow 看三表结构，get_financial_metrics 看多年趋势。
            2. 盈利质量：营收与归母净利润增速是否匹配，经营现金流是否覆盖利润，FCF 与 FCF 转换率是否健康。
            3. 回报与杠杆：ROE、ROA 的驱动（利润率 × 周转 × 杠杆），资产负债率与有息负债压力。
            4. 估值：结合总市值、EPS、BVPS 与历史估值区间判断 PE/PB 水平；用研报一致预期与目标价做参考，但必须注明这是第三方预测。
            5. 横向对比：用 get_competitor_financial_metrics 对比同业，不要自行假设可比公司。
            6. 风险：用 get_announcements 核查业绩预告、减持、定增、诉讼、分红等事件；用 get_stock_dividends 看分红稳定性。

            输出要求：
            - 用「结论 → 关键数据表 → 逻辑与风险」的结构，中文回答。
            - 每个数字注明报告期与单位；缺失数据直接说明，不要用估算值填充。
            - 估值结论必须给出假设和敏感性（例如利润增速、PE 区间）。
            - 任务完成后回复 TERMINATE。

            {a_share_market_context}
            """
        ),
        "toolkits": [
            # 行情与公司
            CNStockUtils.get_stock_data,
            CNStockUtils.get_stock_info,
            CNStockUtils.get_company_info,
            CNStockUtils.get_company_profile,
            CNStockUtils.get_stock_dividends,
            # 三表与财务指标
            CNStockUtils.get_income_stmt,
            CNStockUtils.get_balance_sheet,
            CNStockUtils.get_cash_flow,
            CNStockUtils.get_financial_metrics,
            CNStockUtils.get_competitor_financial_metrics,
            CNStockUtils.get_basic_financials,
            CNStockUtils.get_basic_financials_history,
            # 估值与机构预期
            CNStockUtils.get_historical_market_cap,
            CNStockUtils.get_historical_bvps,
            CNStockUtils.get_analyst_recommendations,
            CNStockUtils.get_analyst_forecast,
            CNStockUtils.get_target_price,
            CNStockUtils.get_research_reports,
            # 公告与新闻
            CNStockUtils.get_announcements,
            CNStockUtils.get_sec_report,
            CNStockUtils.get_company_news,
            CNStockUtils.get_global_news,
            CNStockUtils.get_hot_rank,
            # 图表
            MplFinanceUtils.plot_stock_price_chart,
        ],
    },
    {
        "name": "Expert_Investor",
        "profile": dedent(
            f"""
            Role: A 股研究报告撰写专家（Expert Investor）
            Department: Finance
            Primary Responsibility: Generation of Customized A-Share Financial Analysis Reports

            你的任务是基于中国 A 股上市公司的公开数据，撰写结构完整、数据可追溯的中文证券研究报告。

            工作流程：
            1. 用 get_sec_report 获取巨潮资讯上的年报链接与披露日期（这里的「sec_report」只是历史命名，实际是 cninfo 年报）。
            2. 用 get_financial_metrics / get_income_stmt / get_balance_sheet / get_cash_flow 获取财务数据；
               用 get_competitor_financial_metrics 做同业对比；用 get_research_reports 与 get_target_price 获取机构观点与目标价区间。
            3. 用 ReportChartUtils 生成股价表现图与 PE/EPS 图；用 MplFinanceUtils 生成 K 线图；
               用 ReportLabUtils.build_annual_report 生成 PDF 报告。
            4. 报告中的每个关键数字都要能对应到工具返回值和报告期；预测与目标价必须标注「第三方预测，仅供参考」。
            5. 使用巨潮公告核实分红、业绩预告、减持、定增等重大事项。

            写作要求：
            - 中文撰写，条理清晰，先结论后论据。
            - 明确区分「历史数据」与「预测数据」。
            - 结尾附数据来源、数据截止日期与「不构成投资建议」声明。
            - 任务完成后回复 TERMINATE。

            {a_share_market_context}
            """
        ),
        "toolkits": [
            CNStockUtils.get_sec_report,  # 巨潮年报链接与披露日期
            CNStockUtils.get_financial_metrics,
            CNStockUtils.get_income_stmt,
            CNStockUtils.get_balance_sheet,
            CNStockUtils.get_cash_flow,
            CNStockUtils.get_competitor_financial_metrics,
            CNStockUtils.get_research_reports,
            CNStockUtils.get_target_price,
            CNStockUtils.get_company_news,
            CNStockUtils.get_announcements,
            IPythonUtils.display_image,  # Display image in IPython
            TextUtils.check_text_length,  # Check text length
            ReportLabUtils.build_annual_report,  # Build annual report in designed pdf format
            ReportChartUtils,  # A 股股价表现 / PE-EPS 图表
            MplFinanceUtils,  # K 线图与价格图表
        ],
    },
]
library = {d["name"]: d for d in library}
