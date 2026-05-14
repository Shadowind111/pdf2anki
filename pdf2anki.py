import sys
import os
import json
import sqlite3
import re
import time
import base64
import traceback
import html
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
from http.client import RemoteDisconnected
from datetime import datetime
import fitz  # PyMuPDF

# 禁用 requests 忽略 SSL 校验后的控制台警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QFileDialog,
                             QProgressBar, QTextEdit, QLineEdit,
                             QSpinBox, QComboBox, QFrame, QCheckBox,
                             QGridLayout, QScrollArea)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QFontDatabase, QTextCursor, QPixmap
import genanki

APP_NAME = "pdf2anki"
DB_PATH = "pdf2anki_state.db"


# ==========================================
# 1. 核心文本清洗器
# ==========================================
class TextCleaner:
    @staticmethod
    def post_process_llm_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("【cite：】", "")
        text = re.sub(r'【cite：.*?】', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'```html|```', '', text)
        return text

    @staticmethod
    def compact_plain_text(text: str, max_len: int = 260) -> str:
        if not text:
            return ""
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) <= max_len:
            return text
        return text[:max_len].rstrip() + "..."

    @staticmethod
    def parse_int(value, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        match = re.search(r'\d+', str(value))
        return int(match.group(0)) if match else default

    @staticmethod
    def strip_cloze_and_html(text: str) -> str:
        text = re.sub(r'\{\{c\d+::(.*?)(?:::.*?)?\}\}', r'\1', text or '')
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text


class AppConfig:
    CONFIG_PATH = "pdf2anki_config.json"
    DEFAULTS = {
        "text_api_key": "",
        "text_model": "gpt-4o-mini",
        "text_base_url": "https://api.openai.com/v1",
        "text_use_proxy": False,
        "text_proxy_url": "",
        "vision_reuse_text_api": True,
        "vision_api_key": "",
        "vision_model": "gpt-4o-mini",
        "vision_base_url": "https://api.openai.com/v1",
        "vision_use_proxy": False,
        "vision_proxy_url": "",
        "start_page": 1,
        "end_page": 9999,
        "learning_level": "简单学习"
    }

    @classmethod
    def load(cls) -> dict:
        config = dict(cls.DEFAULTS)
        if not os.path.exists(cls.CONFIG_PATH):
            return config
        try:
            with open(cls.CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                config.update({k: saved.get(k, v) for k, v in cls.DEFAULTS.items()})
        except Exception:
            return config
        return config

    @classmethod
    def save(cls, config: dict) -> None:
        normalized = dict(cls.DEFAULTS)
        normalized.update({k: config.get(k, v) for k, v in cls.DEFAULTS.items()})
        with open(cls.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)


def ensure_db_schema(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, file_path TEXT, total_pages INTEGER, current_page INTEGER DEFAULT 0, status TEXT DEFAULT 'pending')''')
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, chapter TEXT, front_html TEXT, back_html TEXT, card_type TEXT)''')
    cursor.execute("PRAGMA table_info(cards)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    migrations = {
        "source_page": "ALTER TABLE cards ADD COLUMN source_page INTEGER",
        "source_excerpt": "ALTER TABLE cards ADD COLUMN source_excerpt TEXT",
        "knowledge_type": "ALTER TABLE cards ADD COLUMN knowledge_type TEXT",
        "quality_score": "ALTER TABLE cards ADD COLUMN quality_score INTEGER DEFAULT 0",
        "created_at": "ALTER TABLE cards ADD COLUMN created_at TEXT"
    }
    for column, ddl in migrations.items():
        if column not in existing_columns:
            cursor.execute(ddl)
    conn.commit()
    conn.close()


# ==========================================
# 2. 专家级 AI 引擎调用模块 (引入强力重试与过滤机制)
# ==========================================
class AICore:
    def __init__(
            self,
            text_api_key: str,
            text_model: str,
            text_base_url: str,
            vision_api_key: str = "",
            vision_model: str = "",
            vision_base_url: str = "",
            vision_reuse_text_api: bool = True
    ):
        self.text_api_key = text_api_key
        self.text_model = text_model or "gpt-4o-mini"
        self.text_base_url = self._normalize_base_url(text_base_url or "https://api.openai.com/v1")

        if vision_reuse_text_api:
            self.vision_api_key = text_api_key
            self.vision_model = vision_model or self.text_model
            self.vision_base_url = self.text_base_url
        else:
            self.vision_api_key = vision_api_key
            self.vision_model = vision_model or self.text_model
            self.vision_base_url = self._normalize_base_url(vision_base_url or text_base_url or "https://api.openai.com/v1")

        self.text_url = f"{self.text_base_url}/chat/completions"
        self.vision_url = f"{self.vision_base_url}/chat/completions"
        self.vision_failures = 0
        self.vision_disabled_until = 0

        # 建立底层 Session
        self.session = requests.Session()
        retries = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

    def _normalize_base_url(self, base_url: str) -> str:
        base_url = (base_url or "").strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        return base_url

    def _vision_available(self) -> tuple:
        if not self.vision_api_key:
            return False, "未配置视觉/OCR API Key，已跳过视觉解析"
        if time.time() < self.vision_disabled_until:
            wait_seconds = int(self.vision_disabled_until - time.time())
            return False, f"视觉/OCR 接口连续失败，临时跳过视觉解析约 {wait_seconds} 秒"
        return True, None

    def _call_vision_model(self, prompt_text: str, base64_image: str, max_tokens: int, timeout: int) -> tuple:
        ok, unavailable_reason = self._vision_available()
        if not ok:
            return "", unavailable_reason

        headers = {
            "Authorization": f"Bearer {self.vision_api_key}",
            "Content-Type": "application/json",
            "Connection": "close"
        }
        payload = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            "max_tokens": max_tokens
        }

        last_error = ""
        retryable_errors = (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            RemoteDisconnected
        )
        for attempt in range(4):
            try:
                response = self.session.post(self.vision_url, headers=headers, json=payload, timeout=timeout, verify=False)
                response.raise_for_status()
                result = response.json()
                self.vision_failures = 0
                return result['choices'][0]['message']['content'], None
            except retryable_errors as e:
                last_error = f"{type(e).__name__}: {str(e)}"
                time.sleep(min(2 ** attempt, 8))
                continue
            except requests.exceptions.HTTPError as he:
                status = he.response.status_code if he.response is not None else "未知"
                body = he.response.text[:300] if he.response is not None else ""
                if status in (408, 409, 425, 429, 500, 502, 503, 504) and attempt < 3:
                    last_error = f"HTTP {status}: {body}"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                self.vision_failures += 1
                return "", f"视觉/OCR HTTP错误 {status}: {body}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)}"
                time.sleep(min(2 ** attempt, 8))
                continue

        self.vision_failures += 1
        if self.vision_failures >= 3:
            self.vision_disabled_until = time.time() + 180
            return "", f"视觉/OCR 接口连续失败，已暂停视觉解析 3 分钟；最后错误: {last_error}"
        return "", f"视觉/OCR 接口失败，已跳过本次视觉解析；最后错误: {last_error}"

    def _call_text_json(self, system_prompt: str, user_prompt: str, timeout: int = 120) -> tuple:
        headers = {
            "Authorization": f"Bearer {self.text_api_key}",
            "Content-Type": "application/json",
            "Connection": "close"
        }
        payload = {
            "model": self.text_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"}
        }

        for attempt in range(3):
            try:
                response = self.session.post(self.text_url, headers=headers, json=payload, timeout=timeout, verify=False)
                response.raise_for_status()
                content = response.json()['choices'][0]['message']['content'].strip()
                if content.startswith("```"):
                    content = re.sub(r"^```json\s*", "", content)
                    content = re.sub(r"^```\s*", "", content)
                    content = re.sub(r"\s*```$", "", content)
                return json.loads(content), None
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < 2:
                    time.sleep(3)
                    continue
                return {}, f"文本模型网络连接失败: {str(e)}"
            except requests.exceptions.HTTPError as he:
                return {}, f"文本模型 HTTP错误: {he.response.status_code} - {he.response.text}"
            except json.JSONDecodeError:
                return {}, "JSON解析失败: 大模型返回格式不合规"
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                    continue
                return {}, f"文本模型请求异常: {str(e)}"

    def _level_prompt(self, level: str) -> str:
        if level == "简单学习":
            return """
【学习深度：高频核心速记】
只保留对临床诊疗、考试和查房有直接帮助的内容：诊断标准、危险信号、首选检查/治疗、禁忌证、关键数值、分型分层、处理流程。
剔除指南背景、版本历史、作者信息、空泛意义描述和低价值废话。
"""
        return """
【学习深度：全面精通掌握】
在核心诊疗知识之外，补充必要机制、鉴别诊断、特殊人群、罕见但高风险情形、多学科协作要点，并强调前因后果。
仍需剔除指南背景、作者信息、文献出处和没有记忆价值的空话。
"""

    def process_image_with_vision_model(self, base64_image: str) -> tuple:
        """调用多模态代理重构图像，附带垃圾图像过滤指令"""
        prompt_text = """你是一位临床医学专家。请审查这张图像：
1. 如果是水印、出版社Logo、人物照片、二维码、纯装饰线条等与医学临床无关的图片，请务必仅返回 'IGNORE_IMAGE' 这几个字母，不要有任何其他解释。
2. 如果是诊断/治疗流程图、临床路径、数据表格或图示总结，请输出结构化医学内容，并明确标注“适合制卡：是”。流程必须逐项列出，表格必须保留行列含义。
3. 如果是解剖/病理实体图，请提炼最核心的临床特征，并明确标注“适合制卡：视情况”。
4. 输出可以使用 HTML 的 <table>、<ul>、<ol>，但不要编造图中不存在的信息。"""
        return self._call_vision_model(prompt_text, base64_image, max_tokens=1500, timeout=90)

    def ocr_page_with_vision_model(self, base64_image: str, page_num: int) -> tuple:
        """扫描页或乱码页兜底 OCR，并要求模型做轻度医学文本校准。"""
        prompt_text = f"""你是一位医学文献 OCR 校对员。请识别第 {page_num} 页图像中的正文、表格和流程内容。
要求：
1. 保留医学术语、数字、单位、箭头/流程关系，不要扩写或编造。
2. 修正常见 OCR 错字和断行，但不要改变原意。
3. 页眉页脚、水印、目录装饰、出版信息可忽略。
4. 如果有表格或流程，请用 Markdown 或简洁 HTML 保留结构。
5. 只返回校准后的页面文本。"""
        return self._call_vision_model(prompt_text, base64_image, max_tokens=2400, timeout=120)

    def analyze_page_visual_with_vision_model(self, base64_image: str, page_num: int) -> tuple:
        """整页视觉解析：覆盖 PDF 矢量流程图/表格，不只依赖嵌入位图。"""
        prompt_text = f"""你是一位临床医学图表解析专家。请审查第 {page_num} 页整页截图，专门寻找 PDF 文本抽取容易漏掉的医学流程图、表格、算法、阈值图、箭头关系和图示总结。

如果这一页没有医学图表/流程/表格，请仅返回 IGNORE_IMAGE。

如果存在，请输出适合后续制卡的结构化内容：
1. 标题或主题。
2. 图表类型：流程/表格/算法/阈值图/对比图。
3. 所有关键节点、判断条件、阈值、处理动作、结果，必须逐项列出。
4. 复合条件必须完整保留，例如“<25 g/L 且 TSB≥换血阈值”不能只写一半。
5. 不要输出“表2、图3、见上表”这类引用词，除非同时给出表/图的实际内容。
6. 不要编造图中不存在的信息。

可用 <ol>/<ul>/<table> 输出。"""
        return self._call_vision_model(prompt_text, base64_image, max_tokens=2600, timeout=120)

    def extract_knowledge_units(self, text_chunk: str, level: str, source_page: int) -> tuple:
        system_prompt = """你是一位严谨的临床医学知识抽取专家。
任务：从 PDF/OCR 文本中抽取可以被规培医生学习和复习的“医学知识单元”，而不是直接制卡。

过滤黑名单：
1. 指南发布年份、版本历史、编委会、作者单位、基金、文献出处。
2. 没有可操作信息的空话，如“意义重大”“仍需更多研究”“应高度重视”。
3. 与临床诊断、评估、处理、预后、用药、检查无关的信息。
4. “表2、图3、见表、见图、如下表”等引用词本身不是知识点；只有表/图中的实际阈值、条件、处理动作才是知识点。

必须保证：
1. 每条知识单元只来自输入文本，不得编造。
2. source_excerpt 必须摘录能支持该知识的原文短句。
3. knowledge_type 只能取：流程、清单、因果、检查意义、诊断标准、治疗方案、用药剂量、禁忌证、分型分层、鉴别诊断、危险信号、其他。
4. 复合条件必须完整抽取，例如“血清白蛋白<25 g/L 且 TSB≥换血阈值”不能拆丢“且”后面的条件。
5. 对“条件 → 处理动作 → 预期结果/停止标准”的知识，必须把条件、动作和结果分别写清楚。

返回严格 JSON：
{
  "knowledge_units": [
    {
      "id": "u1",
      "knowledge_type": "流程",
      "medical_fact": "经过校准后的医学结论",
      "source_excerpt": "支持该结论的原文短句",
      "source_page": 12,
      "importance": 1
    }
  ]
}"""
        user_prompt = f"""{self._level_prompt(level)}

来源页码：{source_page}
请抽取以下文本中的医学知识单元：

{text_chunk}"""
        data, err = self._call_text_json(system_prompt, user_prompt)
        if err:
            return [], err

        units = []
        for idx, unit in enumerate(data.get("knowledge_units", []), start=1):
            if not isinstance(unit, dict):
                continue
            fact = TextCleaner.compact_plain_text(str(unit.get("medical_fact", "")), 800)
            excerpt = TextCleaner.compact_plain_text(str(unit.get("source_excerpt", "")), 400)
            if not fact or not excerpt:
                continue
            units.append({
                "id": str(unit.get("id") or f"u{idx}"),
                "knowledge_type": str(unit.get("knowledge_type") or "其他"),
                "medical_fact": fact,
                "source_excerpt": excerpt,
                "source_page": TextCleaner.parse_int(unit.get("source_page"), source_page),
                "importance": TextCleaner.parse_int(unit.get("importance"), 2)
            })
        return units, None

    def design_memory_cards(self, knowledge_units: list, level: str) -> tuple:
        system_prompt = """你是一位顶尖临床带教老师和 Anki 卡片设计师。
任务：把医学知识单元改写成适合长期记忆的 Anki Cloze 卡片草稿。

卡片策略：
1. 混合策略：流程/清单/分型/对比表适合一张结构化卡多空；因果/检查意义/单句事实适合拆成多张短卡。
2. 同一知识可以反复挖空：从概念、结果、原因、临床意义等不同角度生成多张短卡。
3. 如果原文段落已经非常适合记忆，可以保留原结构；否则按临床逻辑重组为 <ul>/<ol>/<table>。
4. 卡片 front 必须包含 Anki Cloze 标记 {{c1::...}}。
5. 每张卡 back 必须包含 <strong>临床点拨：</strong>，解释为什么要记。
6. 以专业出题专家视角制卡：要考临床判断和关键阈值，让认真读过原文的人也不能靠熟悉措辞轻松答对。
7. front 必须自带前置背景：疾病/人群/场景/处理节点/检查对象至少交代 1-2 个，不能只给一句无标题的裸知识点。
8. 挖空后必须仍然像一道读得通的题；不要把连接词、限定词、上下文一起挖掉导致句子断裂。

挖空初稿规则：
1. 流程标题下的步骤尽量逐项挖空，体现“流程”。
2. 因果句要双向或多角度挖空，例如“呼吸性酸中毒可引起血压下降”不能只挖前半句。
3. 数字不要和单位一起整块挖掉；应保留提示，例如 CPR {{c1::2}} 分钟。
4. 不挖普通动词、泛泛名词、无提示孤立答案。
5. 严禁挖空“表2、图3、见表、见图、上述、如下”等引用词；这些词不是学习目标。
6. 复合条件要整体考。错误示例：只挖“<25 g/L”却漏掉“且 TSB≥换血阈值”。正确示例：血清白蛋白足月儿 {{c1::<30 g/L}} 或早产儿 {{c2::<25 g/L 且 TSB≥换血阈值}} 时，可考虑静脉输注白蛋白。
7. 条件和处理动作不能在同一张短卡里同时全挖空。应拆成互问卡：
   - 符合哪些标准可以静脉输注白蛋白？
   - 满足这些标准时推荐静脉输注什么？
8. 一张卡只考一个主要判断任务；表格/流程卡可以多空，但每个空都必须有足够上下文。
9. 对“其中 X 是评估风险/指导干预/决定性指标/首选指标/金标准/诊断依据”的句式，必须额外生成一张指标意义卡，挖空 X。例：其中 {{c1::血清总胆红素（TSB）}} 是评估风险及指导干预的决定性指标。
10. 对“应测定 A 或 B，其中 B 是决定性指标”的句式，不能只挖 A；至少要同时考 B，或拆出一张“决定性指标是什么”的卡。
11. “A 或 B”如果是同一个临床动作的并列选项（如应测定 A 或 B、可选择 A 或 B），必须做成一张卡两个空：应测定 {{c1::A}} 或 {{c2::B}}；不要拆成两张卡。
12. 不要把“水平、指标、结果、项目”这类泛词单独作为答案。若原文是“TSB 水平是决定性指标”，答案应是 {{c1::TSB 水平}} 或 {{c1::血清总胆红素（TSB）}}，不能只挖“水平”。
13. 不允许 front 以 {{c1::...}} 开头；挖空前必须有足够提示，例如“新生儿黄疸早期评估：生后 24 小时内肉眼可见黄染时，应测定 ...”。
14. 如果原文靠上一句/标题才能理解，必须把标题或场景补进 front。

返回严格 JSON：
{
  "cards": [
    {
      "front": "HTML + {{c1::挖空}}",
      "back": "<strong>临床点拨：</strong>...",
      "source_page": 12,
      "source_excerpt": "原文依据",
      "knowledge_type": "流程",
      "cloze_rationale": "为什么这样挖",
      "quality_score": 80
    }
  ]
}"""
        user_prompt = f"""{self._level_prompt(level)}

请根据以下知识单元设计 Anki Cloze 卡片草稿。只根据这些知识单元制卡，不要补充外部知识：

{json.dumps({"knowledge_units": knowledge_units}, ensure_ascii=False)}"""
        data, err = self._call_text_json(system_prompt, user_prompt)
        if err:
            return [], err
        return self._normalize_cards(data.get("cards", [])), None

    def review_and_repair_clozes(self, draft_cards: list, knowledge_units: list, level: str) -> tuple:
        system_prompt = """你是严格的 Anki Cloze 质检官，专门修复医学卡片的挖空质量。

必须逐张检查并修复：
1. 只挖了“2分钟”这类孤立答案时，改成保留单位和上下文，例如 CPR {{c1::2}} 分钟。
2. 流程/清单/表格卡必须覆盖主要项目，不能只挖第一项。
3. 因果关系至少覆盖原因和结果中的关键端点，必要时拆成多张短卡。
4. 检查意义类知识要能单独成卡，例如“{{c1::动脉血气}} 对判断危重患者意义重大”。
5. 删除无医学价值、无明确依据、无法从原文支持的卡。
6. 保留 source_page、source_excerpt、knowledge_type；不得编造出处。
7. 删除或修复任何挖空“表2、图3、见表、见图、上述、如下”的卡；引用编号不是记忆目标。
8. 复合临床指征必须完整。遇到 A 且/或 B 的标准，不能只挖 A。要么整体挖完整条件，要么分别生成“A 阈值”和“B 条件”两张有上下文的卡。
9. 条件、治疗动作、结果/停止标准不能在同一张短卡中全部挖掉。应拆成“条件→动作”和“动作←条件”两张互问卡。
10. 每张卡要像专业考试题：临床实用、关键、能区分真正掌握者；但必须忠于原文，不能为了刁钻而编造。
11. 必须检查“其中 X 是评估风险/指导干预/决定性指标/首选指标/金标准/诊断依据”的结构；若 draft_cards 没有挖空 X，必须新增或修复为挖空 X。
12. 对“应测定 A 或 B，其中 B 是决定性指标”，B 必须被考到，不能只挖 A。
13. 对同一临床动作里的“A 或 B”，必须合并成一张双空卡，不要拆成两张独立卡。
14. 若答案只有“水平、指标、项目、结果”等泛词，必须向前补全真正医学名词，例如 TSB 水平、血清总胆红素（TSB）。
15. 如果 front 缺少疾病/人群/场景/处理节点，必须补足前置背景；不得输出让学习者不知道问哪一章、哪类病、哪一步的问题。
16. 修复突兀挖空：保留“在...时/对于.../其中.../应...”等衔接词，必要时重写句子而不是机械挖原文。
17. 不允许卡片以 cloze 开头；不允许整句只剩一个孤立空和泛泛解释。

输出必须是最终可导入 Anki 的严格 JSON：
{
  "cards": [
    {
      "front": "最终 HTML + {{c1::挖空}}",
      "back": "<strong>临床点拨：</strong>...",
      "source_page": 12,
      "source_excerpt": "原文依据",
      "knowledge_type": "流程",
      "cloze_rationale": "修复后的挖空理由",
      "quality_score": 95
    }
  ]
}"""
        user_prompt = f"""{self._level_prompt(level)}

请质检并修复以下卡片。原始知识单元也提供给你用于核对，不允许添加原文没有的医学结论。

{json.dumps({"knowledge_units": knowledge_units, "draft_cards": draft_cards}, ensure_ascii=False)}"""
        data, err = self._call_text_json(system_prompt, user_prompt)
        if err:
            return [], err
        return self._normalize_cards(data.get("cards", [])), None

    def _normalize_cards(self, cards: list) -> list:
        cleaned_cards = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            front = TextCleaner.post_process_llm_text(str(card.get('front', ''))).strip()
            back = TextCleaner.post_process_llm_text(str(card.get('back', ''))).strip()
            front = self._repair_bad_clozes(front)
            if self._has_low_value_cloze(front):
                continue
            if not front or "{{c" not in front:
                continue
            if "<strong>临床点拨" not in back and "<strong>💡 临床点拨" not in back:
                back = f"<strong>临床点拨：</strong> {back}" if back else "<strong>临床点拨：</strong> 请结合来源原文复习。"
            source_page = TextCleaner.parse_int(card.get("source_page"), 0) or None
            quality_score = TextCleaner.parse_int(card.get("quality_score"), 0)
            if re.match(r'^\s*\{\{c\d+::', front):
                ktype = TextCleaner.compact_plain_text(str(card.get("knowledge_type", "医学知识点")), 30)
                front = f"{ktype}：{front}"
            cleaned_cards.append({
                'front': front,
                'back': back,
                'source_page': source_page,
                'source_excerpt': TextCleaner.compact_plain_text(str(card.get("source_excerpt", "")), 500),
                'knowledge_type': TextCleaner.compact_plain_text(str(card.get("knowledge_type", "其他")), 40),
                'cloze_rationale': TextCleaner.compact_plain_text(str(card.get("cloze_rationale", "")), 260),
                'quality_score': quality_score
            })
        return cleaned_cards

    def _repair_bad_clozes(self, front: str) -> str:
        if not front:
            return ""
        # 不允许把“表2/图3/见表”当作答案；直接还原为普通文本。
        front = re.sub(r'\{\{c\d+::((?:表|图)\s*\d+)\}\}', r'\1', front)
        front = re.sub(r'\{\{c\d+::(见(?:表|图)|如下|上述|本表|该表)\}\}', r'\1', front)
        # 数字+单位整块挖空时，尽量只挖数字，保留单位提示。
        front = re.sub(
            r'\{\{c(\d+)::([<>≤≥=]?\s*\d+(?:\.\d+)?(?:\s*[~\-–]\s*\d+(?:\.\d+)?)?)\s*(g/L|mg/dl|mg/dL|μmol/L|mmol/L|分钟|小时|天|周|月|岁|%)\}\}',
            r'{{c\1::\2}} \3',
            front
        )
        return front

    def _has_low_value_cloze(self, front: str) -> bool:
        answers = re.findall(r'\{\{c\d+::(.*?)(?:::.*?)?\}\}', front)
        if not answers:
            return True
        low_value = re.compile(r'^(表|图)\s*\d+$|^见(表|图)$|^如下$|^上述$|^本表$|^该表$')
        meaningful = [a for a in answers if not low_value.match(re.sub(r'\s+', '', a))]
        return len(meaningful) == 0

    def generate_cloze_cards(self, text_chunk: str, level: str) -> tuple:
        return self.generate_cloze_cards_v2(text_chunk, level, None)

    def generate_cloze_cards_v2(self, text_chunk: str, level: str, source_page=None) -> tuple:
        page = source_page or 0
        units, err = self.extract_knowledge_units(text_chunk, level, page)
        if err or not units:
            return [], err
        draft_cards, err = self.design_memory_cards(units, level)
        if err or not draft_cards:
            return [], err
        final_cards, err = self.review_and_repair_clozes(draft_cards, units, level)
        if err:
            return [], err
        final_cards.extend(self._build_deterministic_indicator_cards(units))
        final_cards.extend(self._build_deterministic_or_measurement_cards(units))
        final_cards = self._deduplicate_cards(final_cards)
        return final_cards, None

    def _build_deterministic_indicator_cards(self, knowledge_units: list) -> list:
        cards = []
        for unit in knowledge_units:
            fact = unit.get("medical_fact", "")
            excerpt = unit.get("source_excerpt", "")
            combined = f"{fact} {excerpt}"
            indicator = self._extract_key_indicator(combined)
            if not indicator:
                continue
            if len(indicator) < 2 or re.search(r'^(表|图)\s*\d+$', indicator):
                continue
            if not re.search(r'TSB|胆红素|血气|评分|指数|指标|检查|试验|活性|水平', indicator, flags=re.IGNORECASE):
                continue
            page = unit.get("source_page")
            back = (
                "<strong>临床点拨：</strong> 这类句式里的“决定性指标/关键指标”是临床判断核心，"
                "需要能从适应证或病情描述反推出应测定和依赖的指标。"
            )
            cards.append({
                "front": f"评估风险及指导干预的决定性指标是 {{{{c1::{indicator}}}}}。",
                "back": back,
                "source_page": page,
                "source_excerpt": unit.get("source_excerpt", ""),
                "knowledge_type": "检查意义",
                "cloze_rationale": "确定性补卡：关键指标句式必须挖空指标本身。",
                "quality_score": 95
            })
        return self._normalize_cards(cards)

    def _build_deterministic_or_measurement_cards(self, knowledge_units: list) -> list:
        cards = []
        for unit in knowledge_units:
            combined = f"{unit.get('medical_fact', '')} {unit.get('source_excerpt', '')}"
            pair = self._extract_measurement_pair(combined)
            if not pair:
                continue
            item_a, item_b = pair
            cards.append({
                "front": f"出生后 24 小时内肉眼可见皮肤黄染的新生儿，应测定 {{{{c1::{item_a}}}}} 或 {{{{c2::{item_b}}}}}。",
                "back": "<strong>临床点拨：</strong> 并列检查项目属于同一判断任务，应放在同一张卡用两个空同时考，避免割裂记忆。",
                "source_page": unit.get("source_page"),
                "source_excerpt": unit.get("source_excerpt", ""),
                "knowledge_type": "检查意义",
                "cloze_rationale": "确定性补卡：A 或 B 的并列测定项合并为一张双空卡。",
                "quality_score": 95
            })
        return self._normalize_cards(cards)

    def _extract_measurement_pair(self, text: str):
        text = re.sub(r'\s+', '', text or '')
        patterns = [
            r'应测定([^，。；;]{2,24}?胆红素(?:[（(][A-Za-z0-9+\-/]+[）)])?)或([^，。；;]{2,28}?胆红素(?:[（(][A-Za-z0-9+\-/]+[）)])?)',
            r'测定([^，。；;]{2,24}?胆红素(?:[（(][A-Za-z0-9+\-/]+[）)])?)或([^，。；;]{2,28}?胆红素(?:[（(][A-Za-z0-9+\-/]+[）)])?)'
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            item_a = self._normalize_measurement_name(match.group(1))
            item_b = self._normalize_measurement_name(match.group(2))
            if item_a and item_b and item_a != item_b:
                return item_a, item_b
        return None

    def _normalize_measurement_name(self, value: str) -> str:
        value = (value or "").strip("，,；;。:：、")
        value = re.sub(r'^(应|需|须)?测定', '', value)
        value = value.replace("(", "（").replace(")", "）")
        # 常见 OCR/模型大小写漂移，统一几个高频缩写。
        value = re.sub(r'（tcb）', '（TcB）', value, flags=re.IGNORECASE)
        value = re.sub(r'（tsb）', '（TSB）', value, flags=re.IGNORECASE)
        return value

    def _extract_key_indicator(self, text: str) -> str:
        text = re.sub(r'\s+', '', text or '')
        marker = "\u5176\u4e2d"
        key_phrases = (
            "\u51b3\u5b9a\u6027\u6307\u6807",
            "\u5173\u952e\u6307\u6807",
            "\u9996\u9009\u6307\u6807",
            "\u91d1\u6807\u51c6",
            "\u8bca\u65ad\u4f9d\u636e",
            "\u8bc4\u4f30\u98ce\u9669",
            "\u6307\u5bfc\u5e72\u9884"
        )

        marker_index = text.find(marker)
        if marker_index >= 0:
            tail = text[marker_index + len(marker):]
            verb_positions = [pos for pos in (tail.find("\u662f"), tail.find("\u4e3a")) if pos >= 0]
            if verb_positions:
                verb_index = min(verb_positions)
                indicator = tail[:verb_index].strip("，,；;。:：")
                predicate = tail[verb_index + 1:verb_index + 60]
                if indicator and any(phrase in predicate for phrase in key_phrases):
                    return self._repair_indicator_name(self._expand_abbreviation(indicator, text), text)

        match = re.search(
            r'([A-Za-z][A-Za-z0-9+\-/]{1,10}|[\u4e00-\u9fff]{2,16}(?:[（(][A-Za-z0-9+\-/]{2,10}[）)])?)'
            r'(?:是|为)[^。；;]{0,40}(?:决定性指标|关键指标|首选指标|金标准|诊断依据)',
            text,
            flags=re.IGNORECASE
        )
        if match:
            return self._repair_indicator_name(self._expand_abbreviation(match.group(1).strip(), text), text)
        return ""

    def _repair_indicator_name(self, indicator: str, context: str) -> str:
        indicator = (indicator or "").strip("，,；;。:：、")
        if indicator in ("水平", "指标", "决定性指标", "关键指标"):
            if re.search(r'血清总胆红素[（(]?TSB[）)]?', context, flags=re.IGNORECASE):
                return "血清总胆红素（TSB）"
            if re.search(r'\bTSB\b', context, flags=re.IGNORECASE):
                return "TSB"
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9+\-/]{1,10}水平', indicator, flags=re.IGNORECASE):
            short = indicator[:-2]
            return self._expand_abbreviation(short, context)
        return indicator

    def _expand_abbreviation(self, indicator: str, context: str) -> str:
        if not indicator:
            return ""
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9+\-/]{1,10}', indicator):
            # 从“血清总胆红素(TSB)”这类上下文中恢复中文全称，避免只考缩写。
            match = re.search(r'([\u4e00-\u9fff]{2,20})[（(]\s*' + re.escape(indicator) + r'\s*[）)]', context, flags=re.IGNORECASE)
            if match:
                chinese_name = match.group(1)
                chinese_name = re.split(r'或|和|及|、|测定|检测|复查|为|是|其中', chinese_name)[-1]
                return f"{chinese_name}（{indicator.upper()}）"
            return indicator.upper()
        return indicator

    def _deduplicate_cards(self, cards: list) -> list:
        seen = set()
        deduped = []
        for card in cards:
            key = re.sub(r'\s+', '', card.get("front", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(card)
        return deduped


# ==========================================
# 3. 后台工作线程
# ==========================================
class ProcessingThread(QThread):
    progress_update = pyqtSignal(int, str)
    status_update = pyqtSignal(str)
    log_update = pyqtSignal(str, str)
    preview_update = pyqtSignal(bytes, str)
    finished_signal = pyqtSignal(str)

    def __init__(self, file_path, api_config, start_page, end_page, learning_level, resume_page=None):
        super().__init__()
        self.file_path = file_path
        self.api_config = api_config
        self.start_page = start_page
        self.end_page = end_page
        self.learning_level = learning_level
        self.resume_page = resume_page
        self.is_running = True
        self.db_path = DB_PATH

    def stop(self):
        self.is_running = False

    def _looks_like_bad_text(self, text: str) -> bool:
        stripped = re.sub(r'\s+', '', text or '')
        if len(stripped) < 80:
            return True
        replacement_marks = stripped.count('\ufffd') + stripped.count('□')
        if replacement_marks / max(len(stripped), 1) > 0.03:
            return True
        cjk_or_latin = re.findall(r'[\u4e00-\u9fffA-Za-z]', stripped)
        if len(cjk_or_latin) / max(len(stripped), 1) < 0.45:
            return True
        medical_cues = re.findall(
            r'诊断|治疗|患者|临床|指南|检查|血|病|症|药|剂量|风险|流程|呼吸|循环|CPR|ECG|CT|MRI|PaO2|CO2',
            text or '',
            flags=re.IGNORECASE
        )
        return len(medical_cues) == 0 and len(stripped) < 300

    def _looks_like_visual_knowledge_page(self, page, text: str) -> bool:
        raw_text = text or ""
        cues = re.findall(
            r'表\d+|图\d+|流程|流程图|算法|路径|见表|如下表|阈值|标准|指征|方案|步骤|推荐|'
            r'→|⇒|↓|≥|≤|<|>|TSB|换血|光疗|白蛋白|CPR|复苏|处理',
            raw_text,
            flags=re.IGNORECASE
        )
        if len(cues) >= 2:
            return True
        drawings = page.get_drawings()
        if len(drawings) >= 8 and len(raw_text.strip()) >= 80:
            return True
        if page.get_images(full=True) and re.search(r'表|图|流程|阈值|标准|指征', raw_text):
            return True
        return False

    def _pixmap_to_base64(self, pix) -> str:
        image_bytes = pix.tobytes("jpeg")
        return base64.b64encode(image_bytes).decode("utf-8").replace('\n', '')

    def _pixmap_to_jpeg_bytes(self, pix) -> bytes:
        return pix.tobytes("jpeg")

    def _render_page_for_ocr(self, page, zoom: float = 1.6, max_side: int = 1800) -> str:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        if max(pix.width, pix.height) > max_side:
            ratio = max_side / max(pix.width, pix.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom * ratio, zoom * ratio), alpha=False)
        return self._pixmap_to_base64(pix)

    def _render_page_preview(self, page, zoom: float = 0.9, max_side: int = 900) -> bytes:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        if max(pix.width, pix.height) > max_side:
            ratio = max_side / max(pix.width, pix.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom * ratio, zoom * ratio), alpha=False)
        return self._pixmap_to_jpeg_bytes(pix)

    def _render_page_for_visual_analysis(self, page) -> str:
        pix = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
        if max(pix.width, pix.height) > 1800:
            ratio = 1800.0 / max(pix.width, pix.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.35 * ratio, 1.35 * ratio), alpha=False)
        return self._pixmap_to_base64(pix)

    def _build_toc_index(self, doc) -> list:
        toc_index = []
        try:
            for level, title, page in doc.get_toc(simple=True):
                if page:
                    clean_title = re.sub(r'\s+', ' ', title or '').strip()
                    if clean_title:
                        toc_index.append((int(page), int(level), clean_title))
        except Exception:
            return []
        return sorted(toc_index, key=lambda item: item[0])

    def _topic_for_page(self, toc_index: list, page_num: int) -> str:
        if not toc_index:
            return f"第 {page_num} 页"
        selected = []
        for toc_page, level, title in toc_index:
            if toc_page <= page_num:
                selected = [item for item in selected if item[0] < level]
                selected.append((level, title))
            else:
                break
        if not selected:
            return f"第 {page_num} 页"
        return " / ".join(title for _, title in selected[-3:])

    def _apply_card_context(self, card: dict, topic: str, page_num: int) -> dict:
        topic_text = TextCleaner.compact_plain_text(topic or f"第 {page_num} 页", 120)
        plain_front = TextCleaner.strip_cloze_and_html(card.get("front", ""))
        title_seed = plain_front.split("。", 1)[0].split("；", 1)[0]
        title = TextCleaner.compact_plain_text(title_seed, 52) or topic_text
        front = card.get("front", "")
        if "card-topic" not in front:
            wrapped_tables = front
            if "table-wrap" not in wrapped_tables:
                wrapped_tables = re.sub(r'(<table[\s\S]*?</table>)', r'<div class="table-wrap">\1</div>', wrapped_tables)
            card["front"] = (
                f'<div class="card-topic">{html.escape(topic_text)} | 第 {page_num} 页</div>'
                f'<div class="card-title">{html.escape(title)}</div>'
                f'{wrapped_tables}'
            )
        if not card.get("source_page"):
            card["source_page"] = page_num
        return card

    def _insert_card(self, cursor, task_id: str, chapter: str, card: dict):
        cursor.execute(
            '''INSERT INTO cards (
                task_id, chapter, front_html, back_html, card_type,
                source_page, source_excerpt, knowledge_type, quality_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                task_id,
                chapter,
                card.get('front', ''),
                card.get('back', ''),
                "Cloze",
                card.get('source_page'),
                card.get('source_excerpt', ''),
                card.get('knowledge_type', '其他'),
                card.get('quality_score', 0),
                datetime.now().isoformat(timespec="seconds")
            )
        )

    def run(self):
        ensure_db_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        task_id = f"{os.path.basename(self.file_path)}_{self.learning_level}"
        actual_start_page = self.resume_page if self.resume_page else self.start_page

        self.log_update.emit("info", f"开始处理文档: {os.path.basename(self.file_path)}")
        cursor.execute('INSERT OR IGNORE INTO tasks (task_id, file_path, current_page) VALUES (?, ?, ?)',
                       (task_id, self.file_path, actual_start_page - 1))
        conn.commit()

        doc = None
        try:
            doc = fitz.open(self.file_path)
            actual_end = min(self.end_page, len(doc))
            total_process_pages = actual_end - self.start_page + 1
            toc_index = self._build_toc_index(doc)
            if toc_index:
                self.log_update.emit("info", f"已读取目录层级，将用于卡片前置背景（{len(toc_index)} 条）")
            else:
                self.log_update.emit("warning", "未读取到 PDF 目录，将使用页码作为卡片背景")

            ai_core = AICore(**self.api_config)
            buffer_text = ""

            for page_num in range(actual_start_page - 1, actual_end):
                if not self.is_running:
                    self.status_update.emit("任务已暂停")
                    break

                current_progress = int(((page_num - self.start_page + 1) / total_process_pages) * 100)
                self.progress_update.emit(current_progress, f"正在处理页码: {page_num + 1}/{actual_end}")
                self.status_update.emit("正在提取页面文本与图像...")

                page = doc[page_num]
                page_text = page.get_text()

                if ai_core.vision_api_key and self._looks_like_bad_text(page_text):
                    self.status_update.emit(f"第 {page_num + 1} 页文本质量差，自动启用 OCR 校准...")
                    try:
                        self.preview_update.emit(self._render_page_preview(page), f"第 {page_num + 1} 页 OCR 校准中")
                        page_b64 = self._render_page_for_ocr(page)
                        ocr_text, ocr_err = ai_core.ocr_page_with_vision_model(page_b64, page_num + 1)
                        if ocr_err:
                            self.log_update.emit("error", f"OCR 校准失败: {ocr_err}")
                        elif ocr_text.strip():
                            page_text = ocr_text
                            self.log_update.emit("success", f"第 {page_num + 1} 页已完成 OCR/乱码校准")
                    except Exception as ocr_render_err:
                        self.log_update.emit("error", f"OCR 页面渲染失败: {ocr_render_err}")

                if ai_core.vision_api_key and self._looks_like_visual_knowledge_page(page, page_text):
                    self.status_update.emit(f"第 {page_num + 1} 页疑似含流程/表格，进行整页视觉解析...")
                    try:
                        self.preview_update.emit(self._render_page_preview(page), f"第 {page_num + 1} 页整页图表/流程解析中")
                        visual_b64 = self._render_page_for_visual_analysis(page)
                        visual_text, visual_err = ai_core.analyze_page_visual_with_vision_model(visual_b64, page_num + 1)
                        if visual_err:
                            self.log_update.emit("warning", f"整页视觉解析跳过: {visual_err}")
                        elif visual_text and "IGNORE_IMAGE" not in visual_text:
                            page_text += (
                                "\n\n<div class='visual-note visual-note-page'><strong>整页图表/流程视觉解析</strong><br>"
                                f"{visual_text}</div>\n\n"
                            )
                            self.log_update.emit("success", f"第 {page_num + 1} 页已补充整页图表/流程解析")
                    except Exception as visual_render_err:
                        self.log_update.emit("warning", f"整页视觉解析渲染失败: {visual_render_err}")

                if ai_core.vision_api_key:
                    for img_index, img in enumerate(page.get_images(full=True)):
                        try:
                            xref = img[0]
                            pix = fitz.Pixmap(doc, xref)

                            if pix.n - pix.alpha > 3:
                                pix = fitz.Pixmap(fitz.csRGB, pix)

                            w, h = pix.width, pix.height

                            if w > 150 and h > 150:
                                if max(w, h) > 800:
                                    ratio = 800.0 / max(w, h)
                                    mat = fitz.Matrix(ratio, ratio)
                                    pix = fitz.Pixmap(pix, mat)

                                image_bytes = pix.tobytes("jpeg")

                                if len(image_bytes) > 10000:
                                    self.status_update.emit(f"👁️ 调用 {ai_core.vision_model} 处理复杂图像...")
                                    self.log_update.emit("info",
                                                         f"发现重要图像 (页码 {page_num + 1})，正在解析并已压缩优化...")
                                    self.preview_update.emit(image_bytes, f"第 {page_num + 1} 页嵌入图像解析中")

                                    b64_img = base64.b64encode(image_bytes).decode("utf-8").replace('\n', '')
                                    img_desc, q_err = ai_core.process_image_with_vision_model(b64_img)

                                    if q_err:
                                        self.log_update.emit("error", f"多模态解析失败: {q_err}")
                                    elif "IGNORE_IMAGE" in img_desc:
                                        self.log_update.emit("warning", "已自动识别并丢弃无用图像(水印/Logo等)")
                                    elif img_desc:
                                        page_text += f"\n\n<div class='visual-note'><strong>图表解析与重构</strong><br>{img_desc}</div>\n\n"

                                    time.sleep(1.5)
                            pix = None
                        except Exception as img_err:
                            continue

                buffer_text += f"\n\n[来源页码：{page_num + 1}]\n{page_text}\n"

                while len(buffer_text) > 2200:
                    chunk = buffer_text[:2200]
                    buffer_text = buffer_text[1900:]

                    self.status_update.emit("三阶段生成：抽取知识 → 设计卡片 → 质检挖空...")
                    cards, err = ai_core.generate_cloze_cards_v2(chunk, self.learning_level, page_num + 1)

                    if err:
                        self.log_update.emit("error", f"卡片生成失败: {err}")
                    elif cards:
                        topic = self._topic_for_page(toc_index, page_num + 1)
                        for card in cards:
                            card = self._apply_card_context(card, topic, page_num + 1)
                            self._insert_card(cursor, task_id, f"Page {page_num + 1}", card)
                        conn.commit()
                        self.log_update.emit("success", f"成功提取并生成 {len(cards)} 张临床卡片")
                    else:
                        self.log_update.emit("warning", "本段文本为纯理论背景，未提取出符合实战标准的知识点。")

                    time.sleep(1.5)

                cursor.execute('UPDATE tasks SET current_page = ? WHERE task_id = ?', (page_num + 1, task_id))
                conn.commit()

            if self.is_running and len(buffer_text.strip()) > 50:
                self.status_update.emit("正在处理尾部数据：三阶段生成卡片...")
                cards, err = ai_core.generate_cloze_cards_v2(buffer_text, self.learning_level, actual_end)
                if cards:
                    topic = self._topic_for_page(toc_index, actual_end)
                    for card in cards:
                        card = self._apply_card_context(card, topic, actual_end)
                        self._insert_card(cursor, task_id, "End", card)
                    conn.commit()

            if self.is_running:
                self.status_update.emit("正在打包生成 Anki 文件...")
                self.export_anki(task_id, conn)

        except Exception as e:
            error_trace = traceback.format_exc()
            self.log_update.emit("error", f"致命错误: {str(e)}\n{error_trace}")
            self.status_update.emit("任务异常终止")
        finally:
            if doc: doc.close()
            if conn: conn.close()

    def export_anki(self, task_id, conn):
        css = """
        :root {
            --surface: #fbfbf8;
            --panel: #fffffc;
            --panel-soft: #f4f4ef;
            --ink: #1f2523;
            --muted: #63706b;
            --faint: #dcded5;
            --line: #e7e8df;
            --accent: #2f7667;
            --accent-soft: #e3f0eb;
            --accent-strong: #1f5d51;
            --answer: #8b3f31;
            --answer-soft: #f5e4de;
        }

        html, body {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            background: #f3f3ee;
            background: radial-gradient(circle at 18% 12%, #fffffc 0, #f5f4ef 34%, #ebece4 100%);
            color: var(--ink);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
            font-kerning: normal;
        }

        body {
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }

        .card-shell {
            width: min(92vw, 980px);
            height: min(92vh, 1120px);
            box-sizing: border-box;
            padding: 14px;
        }

        .card-container {
            background: var(--panel);
            width: 100%;
            height: 100%;
            border: 1px solid rgba(31, 37, 35, 0.08);
            border-radius: 22px;
            box-shadow: 0 24px 70px rgba(36, 44, 40, 0.13), 0 2px 8px rgba(36, 44, 40, 0.06);
            box-sizing: border-box;
            padding: 34px 40px 28px;
            position: relative;
            overflow-y: auto;
            overflow-x: hidden;
        }

        .card-container::before {
            content: "";
            position: sticky;
            top: -34px;
            display: block;
            height: 5px;
            margin: -34px -40px 28px;
            background: linear-gradient(90deg, var(--accent), #8b6f47 56%, var(--answer));
            opacity: 0.92;
        }

        .card-container::-webkit-scrollbar { width: 8px; }
        .card-container::-webkit-scrollbar-track { background: transparent; }
        .card-container::-webkit-scrollbar-thumb {
            background: #d6d8cf;
            border-radius: 99px;
        }

        .content-area {
            max-width: 42rem;
            margin: 0 auto;
            font-size: 1.45rem;
            line-height: 1.78;
            letter-spacing: 0;
            font-weight: 400;
        }

        .card-topic {
            display: inline-flex;
            align-items: center;
            width: fit-content;
            max-width: 100%;
            margin-bottom: 0.75rem;
            padding: 0.28rem 0.62rem;
            border: 1px solid var(--line);
            border-radius: 999px;
            background: var(--panel-soft);
            color: var(--muted);
            font-size: 0.78rem;
            font-weight: 650;
            line-height: 1.35;
            letter-spacing: 0.03em;
            text-transform: uppercase;
        }

        .card-title {
            margin: 0 0 1.35rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--line);
            color: var(--ink);
            font-size: 1.12rem;
            font-weight: 750;
            line-height: 1.45;
        }

        strong {
            color: var(--answer);
            font-weight: 720;
        }

        .cloze {
            display: inline;
            color: var(--accent-strong);
            background: linear-gradient(180deg, rgba(227, 240, 235, 0.35), var(--accent-soft));
            border: 1px solid rgba(47, 118, 103, 0.22);
            border-bottom-color: rgba(47, 118, 103, 0.48);
            border-radius: 0.38rem;
            padding: 0.06rem 0.34rem 0.1rem;
            font-weight: 780;
            box-decoration-break: clone;
            -webkit-box-decoration-break: clone;
            transition: background-color 160ms ease-out, color 160ms ease-out, border-color 160ms ease-out;
        }

        .cloze:hover {
            color: #173e38;
            background: #d7ebe3;
            border-color: rgba(47, 118, 103, 0.5);
        }

        p {
            margin: 0 0 1.05rem;
        }

        ul, ol {
            margin: 1rem 0 1.25rem;
            padding-left: 1.35rem;
        }

        li {
            margin-bottom: 0.58rem;
            padding-left: 0.16rem;
        }

        li::marker {
            color: var(--accent);
            font-weight: 700;
        }

        .visual-note {
            margin: 1.15rem 0 1.3rem;
            padding: 0.92rem 1rem;
            border: 1px solid #dde5dc;
            border-radius: 12px;
            background: #f7f8f3;
            color: #3f4945;
            font-size: 0.95rem;
            line-height: 1.58;
        }

        .visual-note strong {
            display: block;
            margin-bottom: 0.35rem;
            color: #6f5d37;
            font-size: 0.78rem;
            font-weight: 780;
            letter-spacing: 0.06em;
        }

        .visual-note-page {
            background: #fbf6ec;
            border-color: #e7dac1;
        }

        .table-wrap {
            width: min(100%, 58rem);
            max-width: calc(100vw - 72px);
            margin: 1.15rem auto 1.35rem;
            overflow-x: auto;
            border: 1px solid var(--line);
            border-radius: 12px;
            background: var(--panel);
            box-shadow: 0 8px 26px rgba(36, 44, 40, 0.06);
            -webkit-overflow-scrolling: touch;
        }

        table {
            width: 100%;
            min-width: 34rem;
            border-collapse: collapse;
            margin: 0;
            background-color: var(--panel);
            color: var(--ink);
            font-size: 0.86rem;
            line-height: 1.42;
            table-layout: auto;
        }

        th {
            background-color: #f0f2eb;
            color: #39433f;
            font-weight: 720;
            padding: 0.58rem 0.7rem;
            text-align: left;
            border-bottom: 1px solid var(--line);
        }

        td {
            padding: 0.58rem 0.7rem;
            border-bottom: 1px solid #eeeeea;
            color: #38423f;
            vertical-align: top;
        }

        tr:last-child td { border-bottom: none; }
        tr:nth-child(even) td { background: #fbfbf7; }
        tr:hover td { background-color: #eef5f1; }

        .back-section {
            margin-top: 1.85rem;
            padding: 1.15rem 1.2rem;
            border: 1px solid #dce7df;
            border-radius: 14px;
            background: #f4faf7;
            color: #33413d;
            font-size: 1.05rem;
            line-height: 1.65;
        }

        .back-section::before {
            content: "临床点拨";
            display: block;
            margin-bottom: 0.55rem;
            color: var(--accent-strong);
            font-size: 0.76rem;
            font-weight: 780;
            letter-spacing: 0.08em;
        }

        .source-section {
            margin-top: 1rem;
            padding-top: 0.9rem;
            border-top: 1px solid #dce7df;
            color: #69766f;
            font-size: 0.84rem;
            line-height: 1.58;
        }

        .source-section strong {
            color: #42504b;
            font-weight: 720;
        }

        .card-brand {
            margin-top: 1.45rem;
            color: #a9afa6;
            font-size: 0.72rem;
            font-weight: 680;
            letter-spacing: 0.14em;
            text-align: right;
            text-transform: uppercase;
        }

        @media (max-width: 700px) {
            body { align-items: stretch; }
            .card-shell {
                width: 100vw;
                height: 100vh;
                padding: 9px;
            }
            .card-container {
                border-radius: 18px;
                padding: 24px 22px 22px;
            }
            .card-container::before {
                top: -24px;
                margin: -24px -22px 22px;
            }
            .content-area {
                max-width: none;
                font-size: 1.22rem;
                line-height: 1.72;
            }
            .card-topic {
                font-size: 0.68rem;
                white-space: normal;
            }
            .card-title {
                font-size: 1rem;
                margin-bottom: 1.05rem;
            }
            .cloze {
                padding: 0.04rem 0.24rem 0.08rem;
            }
            .table-wrap {
                max-width: calc(100vw - 44px);
                margin-left: 0;
                margin-right: 0;
                border-radius: 10px;
            }
            table {
                min-width: 30rem;
                font-size: 0.72rem;
                line-height: 1.32;
            }
            th, td {
                padding: 0.42rem 0.5rem;
            }
            .back-section {
                margin-top: 1.3rem;
                padding: 0.9rem 0.95rem;
                font-size: 0.96rem;
            }
            .source-section {
                font-size: 0.76rem;
            }
        }
        """

        model = genanki.Model(
            1607392320, 'pdf2anki Clinical Cloze Model',
            fields=[{'name': 'Front'}, {'name': 'Back'}],
            templates=[{
                'name': 'Clinical Card',
                'qfmt': '<main class="card-shell"><section class="card-container"><article class="content-area">{{cloze:Front}}<div class="card-brand">pdf2anki</div></article></section></main>',
                'afmt': '<main class="card-shell"><section class="card-container"><article class="content-area">{{cloze:Front}}<div class="back-section">{{Back}}</div><div class="card-brand">pdf2anki</div></article></section></main>',
            }],
            css=css,
            model_type=genanki.Model.CLOZE
        )

        deck = genanki.Deck(2059400110, 'pdf2anki 医学智能重构题库')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT front_html, back_html, source_page, source_excerpt, knowledge_type, quality_score
            FROM cards WHERE task_id = ?
        ''', (task_id,))
        rows = cursor.fetchall()

        if not rows:
            self.log_update.emit("warning", "当前区间未生成任何符合临床标准的卡片。")
            return

        for row in rows:
            front_html, back_html, source_page, source_excerpt, knowledge_type, quality_score = row
            source_parts = []
            if source_page:
                source_parts.append(f"<strong>来源页码：</strong>第 {source_page} 页")
            if knowledge_type:
                source_parts.append(f"<strong>知识类型：</strong>{html.escape(str(knowledge_type))}")
            if quality_score:
                source_parts.append(f"<strong>挖空质检：</strong>{quality_score}/100")
            if source_excerpt:
                safe_excerpt = html.escape(str(source_excerpt))
                source_parts.append(f"<strong>原文依据：</strong>{safe_excerpt}")
            if source_parts:
                back_html = f"{back_html}<div class=\"source-section\">{'<br>'.join(source_parts)}</div>"
            note = genanki.Note(model=model, fields=[front_html, back_html])
            deck.add_note(note)

        output_name = os.path.abspath(f"pdf2anki_output_{datetime.now().strftime('%Y%m%d%H%M')}.apkg")
        genanki.Package(deck).write_to_file(output_name)
        self.status_update.emit("打包完成")
        self.finished_signal.emit(output_name)


# ==========================================
# 4. 主 GUI 窗口
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = AppConfig.load()
        self.init_db_schema()
        self.initUI()
        self.resume_page = None
        self.apply_config()

    def init_db_schema(self):
        ensure_db_schema(DB_PATH)

    def apply_theme(self):
        for font_path in (
                r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
                r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\Deng.ttf",
        ):
            if os.path.exists(font_path):
                QFontDatabase.addApplicationFont(font_path)
        QApplication.instance().setFont(QFont("Microsoft YaHei UI", 9))
        self.setStyleSheet("""
            QWidget {
                color: #102033;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Segoe UI", system-ui, sans-serif;
                font-size: 13px;
            }
            QMainWindow {
                background-color: #eef3f8;
            }
            QFrame#leftRail {
                background-color: #102033;
                border-radius: 14px;
            }
            QLabel#appTitle {
                color: #f6f8fb;
                font-size: 22px;
                font-weight: 800;
            }
            QLabel#appSubtitle {
                color: #b8c5d6;
                font-size: 12px;
                line-height: 1.4;
            }
            QLabel#railSectionLabel {
                color: #8ea0b8;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.5px;
                text-transform: uppercase;
            }
            QFrame#stepItem {
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 10px;
            }
            QLabel#stepNumber {
                color: #102033;
                background-color: #dbe7f4;
                border-radius: 12px;
                font-size: 12px;
                font-weight: 800;
                min-width: 24px;
                min-height: 24px;
                max-width: 24px;
                max-height: 24px;
            }
            QLabel#stepTitle {
                color: #f6f8fb;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#stepHint {
                color: #9fb0c4;
                font-size: 11px;
            }
            QFrame#panel {
                background-color: #fbfcfe;
                border: 1px solid #d8e2ed;
                border-radius: 12px;
            }
            QFrame#summaryBlock {
                background-color: #f3f7fb;
                border: 1px solid #d8e2ed;
                border-radius: 9px;
            }
            QFrame#statusPanel {
                background-color: #e7f0fb;
                border: 1px solid #bdd2ec;
                border-radius: 12px;
            }
            QLabel#panelTitle {
                color: #102033;
                font-size: 15px;
                font-weight: 800;
            }
            QLabel#panelCaption {
                color: #5f6f82;
                font-size: 12px;
            }
            QLabel#fieldLabel {
                color: #37475a;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#mutedText {
                color: #6d7c8f;
                font-size: 12px;
            }
            QLabel#statusText {
                color: #173b62;
                font-size: 14px;
                font-weight: 800;
            }
            QLabel#summaryValue {
                color: #102033;
                font-size: 13px;
                font-weight: 800;
            }
            QLabel#summaryLabel {
                color: #607185;
                font-size: 11px;
                font-weight: 700;
            }
            QLineEdit, QSpinBox, QComboBox {
                min-height: 34px;
                padding: 5px 9px;
                border: 1px solid #c6d2df;
                border-radius: 8px;
                background-color: #ffffff;
                color: #102033;
                selection-background-color: #2563eb;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border: 1px solid #2563eb;
                background-color: #ffffff;
            }
            QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
                background-color: #eef2f6;
                color: #8a98a8;
                border-color: #d8e2ed;
            }
            QCheckBox {
                color: #37475a;
                spacing: 8px;
                font-weight: 600;
            }
            QCheckBox:disabled {
                color: #8a98a8;
            }
            QPushButton {
                min-height: 38px;
                padding: 7px 16px;
                border: 1px solid transparent;
                border-radius: 9px;
                background-color: #2563eb;
                color: #ffffff;
                font-weight: 800;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #1d56d6;
            }
            QPushButton:pressed {
                background-color: #1746ad;
            }
            QPushButton:focus {
                border: 1px solid #102033;
            }
            QPushButton:disabled {
                background-color: #d8e2ed;
                color: #7d8c9f;
            }
            QPushButton#secondaryButton {
                background-color: #ffffff;
                color: #173b62;
                border: 1px solid #c6d2df;
            }
            QPushButton#secondaryButton:hover {
                background-color: #f2f6fb;
                border-color: #9fb4cc;
            }
            QPushButton#successButton {
                background-color: #059669;
                color: #ffffff;
            }
            QPushButton#successButton:hover {
                background-color: #047857;
            }
            QPushButton#warningButton {
                background-color: #b7791f;
                color: #ffffff;
            }
            QPushButton#warningButton:hover {
                background-color: #966513;
            }
            QPushButton#dangerButton {
                background-color: #d14343;
                color: #ffffff;
            }
            QPushButton#dangerButton:hover {
                background-color: #b83232;
            }
            QProgressBar {
                min-height: 18px;
                border: 1px solid #c6d2df;
                border-radius: 8px;
                background-color: #f4f7fa;
                text-align: center;
                color: #173b62;
                font-weight: 700;
            }
            QProgressBar::chunk {
                background-color: #059669;
                border-radius: 7px;
            }
            QTextEdit {
                background-color: #111827;
                color: #d5dde8;
                border: 1px solid #233044;
                border-radius: 10px;
                padding: 10px;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 12px;
                line-height: 1.5;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
        """)

    def make_panel(self, title: str, caption: str = "") -> tuple:
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout()
        panel_layout.setContentsMargins(16, 14, 16, 16)
        panel_layout.setSpacing(12)

        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("panelTitle")
        header_layout.addWidget(title_label)
        if caption:
            caption_label = QLabel(caption)
            caption_label.setObjectName("panelCaption")
            caption_label.setWordWrap(True)
            header_layout.addWidget(caption_label)
        panel_layout.addLayout(header_layout)
        panel.setLayout(panel_layout)
        return panel, panel_layout

    def make_field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def add_field(self, layout, row: int, column: int, label_text: str, widget, column_span: int = 1):
        layout.addWidget(self.make_field_label(label_text), row, column)
        layout.addWidget(widget, row + 1, column, 1, column_span)

    def make_field_block(self, label_text: str, widget) -> QWidget:
        block = QWidget()
        block_layout = QVBoxLayout()
        block_layout.setContentsMargins(0, 0, 0, 0)
        block_layout.setSpacing(4)
        block_layout.addWidget(self.make_field_label(label_text))
        block_layout.addWidget(widget)
        block.setLayout(block_layout)
        return block

    def create_step_item(self, number: str, title: str, hint: str) -> QFrame:
        item = QFrame()
        item.setObjectName("stepItem")
        item_layout = QHBoxLayout()
        item_layout.setContentsMargins(10, 10, 10, 10)
        item_layout.setSpacing(10)

        number_label = QLabel(number)
        number_label.setObjectName("stepNumber")
        number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("stepTitle")
        hint_label = QLabel(hint)
        hint_label.setObjectName("stepHint")
        hint_label.setWordWrap(True)
        text_layout.addWidget(title_label)
        text_layout.addWidget(hint_label)

        item_layout.addWidget(number_label)
        item_layout.addLayout(text_layout)
        item.setLayout(item_layout)
        return item

    def make_summary_block(self, label_text: str, value_text: str) -> tuple:
        block = QFrame()
        block.setObjectName("summaryBlock")
        block_layout = QVBoxLayout()
        block_layout.setContentsMargins(10, 8, 10, 8)
        block_layout.setSpacing(2)
        label = QLabel(label_text)
        label.setObjectName("summaryLabel")
        value = QLabel(value_text)
        value.setObjectName("summaryValue")
        value.setWordWrap(True)
        block_layout.addWidget(label)
        block_layout.addWidget(value)
        block.setLayout(block_layout)
        return block, value

    def clean_display_text(self, text: str) -> str:
        replacements = {
            "🟢": "",
            "✅": "",
            "⚠️": "",
            "⚠": "",
            "💾": "",
            "💡": "",
            "⚡": "",
            "🚀": "",
            "🔄": "",
            "⏸️": "",
            "🎉": "",
            "📁": "",
        }
        cleaned = str(text or "")
        for old, new in replacements.items():
            cleaned = cleaned.replace(old, new)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def update_summary(self):
        if not hasattr(self, "summary_file_label"):
            return
        file_text = os.path.basename(self.selected_file) if self.selected_file else "未选择"
        self.summary_file_label.setText(file_text)
        self.summary_pages_label.setText(f"{self.start_page_spin.value()}-{self.end_page_spin.value()}")
        self.summary_level_label.setText(self.level_combo.currentText())
        self.summary_progress_label.setText(f"{self.progress_bar.value()}%")

    def initUI(self):
        self.setWindowTitle('pdf2anki 医学 PDF 转 Anki 卡片工作站')
        self.resize(1280, 820)
        self.setMinimumSize(1120, 720)
        self.apply_theme()

        main_widget = QWidget()
        outer_layout = QHBoxLayout()
        outer_layout.setContentsMargins(18, 18, 18, 18)
        outer_layout.setSpacing(14)

        rail = QFrame()
        rail.setObjectName("leftRail")
        rail.setFixedWidth(210)
        rail_layout = QVBoxLayout()
        rail_layout.setContentsMargins(16, 18, 16, 18)
        rail_layout.setSpacing(14)

        title_label = QLabel("pdf2anki")
        title_label.setObjectName("appTitle")
        subtitle_label = QLabel("医学 PDF 转 Anki Cloze 卡片工作站")
        subtitle_label.setObjectName("appSubtitle")
        subtitle_label.setWordWrap(True)
        rail_layout.addWidget(title_label)
        rail_layout.addWidget(subtitle_label)
        rail_layout.addSpacing(10)

        stage_label = QLabel("WORKFLOW")
        stage_label.setObjectName("railSectionLabel")
        rail_layout.addWidget(stage_label)
        self.step_pdf = self.create_step_item("1", "选择 PDF", "载入教材、指南或论文")
        self.step_config = self.create_step_item("2", "配置模型", "文本与视觉接口")
        self.step_generate = self.create_step_item("3", "生成卡片", "断点续跑与导出")
        rail_layout.addWidget(self.step_pdf)
        rail_layout.addWidget(self.step_config)
        rail_layout.addWidget(self.step_generate)
        rail_layout.addStretch()

        rail_note = QLabel("先用 3-5 页小范围试跑，确认 OCR 与 Cloze 质量后再处理整本资料。")
        rail_note.setObjectName("appSubtitle")
        rail_note.setWordWrap(True)
        rail_layout.addWidget(rail_note)
        rail.setLayout(rail_layout)

        center_column = QWidget()
        center_column_layout = QVBoxLayout()
        center_column_layout.setContentsMargins(0, 0, 0, 0)
        center_column_layout.setSpacing(12)

        center_content = QWidget()
        center_layout = QVBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(12)

        status_frame = QFrame()
        status_frame.setObjectName("statusPanel")
        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(16, 12, 16, 12)
        status_layout.setSpacing(12)
        status_text_layout = QVBoxLayout()
        status_text_layout.setSpacing(2)
        status_caption = QLabel("当前状态")
        status_caption.setObjectName("summaryLabel")
        self.global_status_label = QLabel("系统就绪，请选择 PDF 并确认 API 配置。")
        self.global_status_label.setObjectName("statusText")
        self.global_status_label.setWordWrap(True)
        status_text_layout.addWidget(status_caption)
        status_text_layout.addWidget(self.global_status_label)
        self.status_meta_label = QLabel("配置会自动保存到本地，生成过程支持暂停和断点继续。")
        self.status_meta_label.setObjectName("mutedText")
        self.status_meta_label.setWordWrap(True)
        status_layout.addLayout(status_text_layout, 1)
        status_layout.addWidget(self.status_meta_label, 0)
        status_frame.setLayout(status_layout)
        center_layout.addWidget(status_frame)

        task_panel, task_layout = self.make_panel("任务输入", "选择来源 PDF，并设置本次生成的页码范围和学习深度。")
        task_grid = QGridLayout()
        task_grid.setHorizontalSpacing(12)
        task_grid.setVerticalSpacing(8)

        self.btn_select = QPushButton("选择教材 PDF")
        self.btn_select.setObjectName("secondaryButton")
        self.btn_select.setMinimumWidth(150)
        self.btn_select.clicked.connect(self.select_file)

        file_box = QFrame()
        file_box.setObjectName("panel")
        file_box_layout = QVBoxLayout()
        file_box_layout.setContentsMargins(12, 8, 12, 8)
        file_box_layout.setSpacing(4)
        file_caption = QLabel("已选择文件")
        file_caption.setObjectName("summaryLabel")
        self.file_label = QLabel("未载入文件")
        self.file_label.setObjectName("summaryValue")
        self.file_label.setWordWrap(True)
        self.resume_badge = QLabel("")
        self.resume_badge.setObjectName("mutedText")
        self.resume_badge.setWordWrap(True)
        file_box_layout.addWidget(file_caption)
        file_box_layout.addWidget(self.file_label)
        file_box_layout.addWidget(self.resume_badge)
        file_box.setLayout(file_box_layout)

        self.start_page_spin = QSpinBox()
        self.start_page_spin.setRange(1, 9999)
        self.start_page_spin.setValue(1)
        self.start_page_spin.valueChanged.connect(self.check_resume_state)
        self.start_page_spin.valueChanged.connect(self.update_summary)
        self.end_page_spin = QSpinBox()
        self.end_page_spin.setRange(1, 9999)
        self.end_page_spin.setValue(9999)
        self.end_page_spin.valueChanged.connect(self.check_resume_state)
        self.end_page_spin.valueChanged.connect(self.update_summary)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["简单学习", "充分学习"])
        self.level_combo.currentTextChanged.connect(self.check_resume_state)
        self.level_combo.currentTextChanged.connect(self.update_summary)

        task_grid.addWidget(self.btn_select, 0, 0, 2, 1)
        task_grid.addWidget(file_box, 0, 1, 2, 3)
        self.add_field(task_grid, 2, 0, "起始页", self.start_page_spin)
        self.add_field(task_grid, 2, 1, "结束页", self.end_page_spin)
        self.add_field(task_grid, 2, 2, "学习深度", self.level_combo, 2)
        task_grid.setColumnStretch(1, 1)
        task_grid.setColumnStretch(2, 1)
        task_grid.setColumnStretch(3, 1)
        task_layout.addLayout(task_grid)
        center_layout.addWidget(task_panel)

        config_panel, config_layout = self.make_panel("API 与策略配置", "文本模型负责知识抽取与卡片生成；视觉/OCR 用于扫描页、表格和流程图。")

        self.text_api_key_input = QLineEdit()
        self.text_api_key_input.setPlaceholderText("文本模型 API Key")
        self.text_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.text_model_input = QLineEdit()
        self.text_model_input.setPlaceholderText("如 gpt-4o-mini")
        self.text_use_proxy_checkbox = QCheckBox("文本使用代理")
        self.text_proxy_url_input = QLineEdit()
        self.text_proxy_url_input.setPlaceholderText("文本代理 Base URL")
        self.text_base_url_input = QLineEdit()
        self.text_base_url_input.setPlaceholderText("https://api.openai.com/v1")

        self.vision_reuse_text_checkbox = QCheckBox("视觉/OCR 复用文本接口")
        self.vision_api_key_input = QLineEdit()
        self.vision_api_key_input.setPlaceholderText("不复用时填写")
        self.vision_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.vision_model_input = QLineEdit()
        self.vision_model_input.setPlaceholderText("视觉模型名称")
        self.vision_use_proxy_checkbox = QCheckBox("视觉使用代理")
        self.vision_proxy_url_input = QLineEdit()
        self.vision_proxy_url_input.setPlaceholderText("视觉代理 Base URL")
        self.vision_base_url_input = QLineEdit()
        self.vision_base_url_input.setPlaceholderText("https://api.openai.com/v1")

        text_header = QLabel("文本模型")
        text_header.setObjectName("panelTitle")
        vision_header = QLabel("视觉 / OCR")
        vision_header.setObjectName("panelTitle")

        text_first_row = QHBoxLayout()
        text_first_row.setSpacing(10)
        text_first_row.addWidget(self.make_field_block("API Key", self.text_api_key_input), 1)
        text_first_row.addWidget(self.make_field_block("模型", self.text_model_input), 1)
        text_url_row = QHBoxLayout()
        text_url_row.setSpacing(10)
        text_url_row.addWidget(self.make_field_block("官方 Base URL", self.text_base_url_input), 1)
        text_url_row.addWidget(self.make_field_block("代理 Base URL", self.text_proxy_url_input), 1)

        vision_first_row = QHBoxLayout()
        vision_first_row.setSpacing(10)
        vision_first_row.addWidget(self.make_field_block("API Key", self.vision_api_key_input), 1)
        vision_first_row.addWidget(self.make_field_block("模型", self.vision_model_input), 1)
        vision_url_row = QHBoxLayout()
        vision_url_row.setSpacing(10)
        vision_url_row.addWidget(self.make_field_block("官方 Base URL", self.vision_base_url_input), 1)
        vision_url_row.addWidget(self.make_field_block("代理 Base URL", self.vision_proxy_url_input), 1)

        text_column = QVBoxLayout()
        text_column.setSpacing(8)
        text_column.addWidget(text_header)
        text_column.addLayout(text_first_row)
        text_column.addWidget(self.text_use_proxy_checkbox)
        text_column.addLayout(text_url_row)

        vision_column = QVBoxLayout()
        vision_column.setSpacing(8)
        vision_column.addWidget(vision_header)
        vision_column.addWidget(self.vision_reuse_text_checkbox)
        vision_column.addLayout(vision_first_row)
        vision_column.addWidget(self.vision_use_proxy_checkbox)
        vision_column.addLayout(vision_url_row)

        config_columns = QHBoxLayout()
        config_columns.setSpacing(18)
        config_columns.addLayout(text_column, 1)
        config_columns.addLayout(vision_column, 1)
        config_layout.addLayout(config_columns)

        self.text_use_proxy_checkbox.stateChanged.connect(self.update_api_field_states)
        self.vision_use_proxy_checkbox.stateChanged.connect(self.update_api_field_states)
        self.vision_reuse_text_checkbox.stateChanged.connect(self.update_api_field_states)
        center_layout.addWidget(config_panel)

        run_panel, run_layout = self.make_panel("生成控制", "进度、日志和断点续跑集中在这里。")

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        run_layout.addWidget(self.progress_bar)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(96)
        self.log_area.setMaximumHeight(120)
        run_layout.addWidget(self.log_area, 1)

        control_layout = QHBoxLayout()
        control_layout.setSpacing(10)

        self.btn_start = QPushButton("开始生成卡片")
        self.btn_start.setMinimumHeight(42)
        self.btn_start.clicked.connect(lambda: self.start_processing(resume=False))

        self.btn_resume = QPushButton("继续断点")
        self.btn_resume.setObjectName("successButton")
        self.btn_resume.setMinimumHeight(42)
        self.btn_resume.clicked.connect(lambda: self.start_processing(resume=True))
        self.btn_resume.setVisible(False)

        self.btn_stop = QPushButton("暂停任务")
        self.btn_stop.setObjectName("dangerButton")
        self.btn_stop.setMinimumHeight(42)
        self.btn_stop.clicked.connect(self.stop_processing)
        self.btn_stop.setEnabled(False)

        control_layout.addStretch()
        control_layout.addWidget(self.btn_resume)
        control_layout.addWidget(self.btn_stop)
        control_layout.addWidget(self.btn_start)
        run_layout.addLayout(control_layout)

        right_layout = QVBoxLayout()
        right_layout.setSpacing(12)

        summary_panel, summary_layout = self.make_panel("运行摘要", "快速确认本次任务的关键状态。")
        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(8)
        summary_grid.setVerticalSpacing(8)
        file_summary, self.summary_file_label = self.make_summary_block("文件", "未选择")
        page_summary, self.summary_pages_label = self.make_summary_block("页码", "1-9999")
        level_summary, self.summary_level_label = self.make_summary_block("学习深度", "简单学习")
        progress_summary, self.summary_progress_label = self.make_summary_block("进度", "0%")
        summary_grid.addWidget(file_summary, 0, 0)
        summary_grid.addWidget(page_summary, 0, 1)
        summary_grid.addWidget(level_summary, 1, 0)
        summary_grid.addWidget(progress_summary, 1, 1)
        summary_layout.addLayout(summary_grid)
        right_layout.addWidget(summary_panel)

        preview_group, preview_layout = self.make_panel("实时识图", "OCR、表格或流程图视觉解析时会显示页面预览。")
        preview_group.setFixedWidth(310)
        self.preview_title_label = QLabel("尚未开始视觉解析")
        self.preview_title_label.setWordWrap(True)
        self.preview_title_label.setObjectName("summaryValue")
        self.preview_image_label = QLabel("OCR/表格/流程图识别时显示")
        self.preview_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image_label.setMinimumSize(270, 210)
        self.preview_image_label.setMaximumHeight(300)
        self.preview_image_label.setStyleSheet(
            "background:#f4f7fa; border:1px dashed #b8c6d6; border-radius:10px; color:#7d8c9f; padding:10px;"
        )
        preview_layout.addWidget(self.preview_title_label)
        preview_layout.addWidget(self.preview_image_label)
        preview_layout.addStretch()
        right_layout.addWidget(preview_group, 1)

        center_content.setLayout(center_layout)
        center_scroll = QScrollArea()
        center_scroll.setWidgetResizable(True)
        center_scroll.setFrameShape(QFrame.Shape.NoFrame)
        center_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        center_scroll.setWidget(center_content)
        center_column_layout.addWidget(center_scroll, 1)
        center_column_layout.addWidget(run_panel, 0)
        center_column.setLayout(center_column_layout)
        outer_layout.addWidget(rail)
        outer_layout.addWidget(center_column, 1)
        outer_layout.addLayout(right_layout, 0)
        main_widget.setLayout(outer_layout)
        self.setCentralWidget(main_widget)
        self.selected_file = None
        self.thread = None
        self.update_summary()

    def apply_config(self):
        self.text_api_key_input.setText(str(self.config.get("text_api_key", "")))
        self.text_model_input.setText(str(self.config.get("text_model", "gpt-4o-mini")))
        self.text_base_url_input.setText(str(self.config.get("text_base_url", "https://api.openai.com/v1")))
        self.text_use_proxy_checkbox.setChecked(bool(self.config.get("text_use_proxy", False)))
        self.text_proxy_url_input.setText(str(self.config.get("text_proxy_url", "")))

        self.vision_reuse_text_checkbox.setChecked(bool(self.config.get("vision_reuse_text_api", True)))
        self.vision_api_key_input.setText(str(self.config.get("vision_api_key", "")))
        self.vision_model_input.setText(str(self.config.get("vision_model", "gpt-4o-mini")))
        self.vision_base_url_input.setText(str(self.config.get("vision_base_url", "https://api.openai.com/v1")))
        self.vision_use_proxy_checkbox.setChecked(bool(self.config.get("vision_use_proxy", False)))
        self.vision_proxy_url_input.setText(str(self.config.get("vision_proxy_url", "")))

        self.start_page_spin.setValue(int(self.config.get("start_page", 1) or 1))
        self.end_page_spin.setValue(int(self.config.get("end_page", 9999) or 9999))
        level = str(self.config.get("learning_level", "简单学习"))
        index = self.level_combo.findText(level)
        if index >= 0:
            self.level_combo.setCurrentIndex(index)
        self.update_api_field_states()
        if self.text_api_key_input.text():
            self.update_global_status("已载入本地配置，请选择 PDF 文档开始生成。")
        self.update_summary()

    def collect_config(self) -> dict:
        return {
            "text_api_key": self.text_api_key_input.text().strip(),
            "text_model": self.text_model_input.text().strip() or "gpt-4o-mini",
            "text_base_url": self.text_base_url_input.text().strip() or "https://api.openai.com/v1",
            "text_use_proxy": self.text_use_proxy_checkbox.isChecked(),
            "text_proxy_url": self.text_proxy_url_input.text().strip(),
            "vision_reuse_text_api": self.vision_reuse_text_checkbox.isChecked(),
            "vision_api_key": self.vision_api_key_input.text().strip(),
            "vision_model": self.vision_model_input.text().strip() or self.text_model_input.text().strip() or "gpt-4o-mini",
            "vision_base_url": self.vision_base_url_input.text().strip() or "https://api.openai.com/v1",
            "vision_use_proxy": self.vision_use_proxy_checkbox.isChecked(),
            "vision_proxy_url": self.vision_proxy_url_input.text().strip(),
            "start_page": self.start_page_spin.value(),
            "end_page": self.end_page_spin.value(),
            "learning_level": self.level_combo.currentText()
        }

    def _effective_base_url(self, base_url: str, use_proxy: bool, proxy_url: str) -> str:
        if use_proxy and proxy_url.strip():
            return proxy_url.strip()
        return base_url.strip() or "https://api.openai.com/v1"

    def build_api_config(self) -> dict:
        config = self.collect_config()
        text_base_url = self._effective_base_url(
            config["text_base_url"],
            config["text_use_proxy"],
            config["text_proxy_url"]
        )
        vision_base_url = self._effective_base_url(
            config["vision_base_url"],
            config["vision_use_proxy"],
            config["vision_proxy_url"]
        )
        return {
            "text_api_key": config["text_api_key"],
            "text_model": config["text_model"],
            "text_base_url": text_base_url,
            "vision_api_key": config["vision_api_key"],
            "vision_model": config["vision_model"],
            "vision_base_url": vision_base_url,
            "vision_reuse_text_api": config["vision_reuse_text_api"]
        }

    def update_api_field_states(self):
        text_uses_proxy = self.text_use_proxy_checkbox.isChecked()
        self.text_base_url_input.setEnabled(not text_uses_proxy)
        self.text_proxy_url_input.setEnabled(text_uses_proxy)

        reuse_text = self.vision_reuse_text_checkbox.isChecked()
        self.vision_api_key_input.setEnabled(not reuse_text)
        self.vision_use_proxy_checkbox.setEnabled(not reuse_text)
        self.vision_base_url_input.setEnabled((not reuse_text) and (not self.vision_use_proxy_checkbox.isChecked()))
        self.vision_proxy_url_input.setEnabled((not reuse_text) and self.vision_use_proxy_checkbox.isChecked())
        self.vision_model_input.setEnabled(True)
        self.status_meta_label.setText(
            "视觉/OCR 当前复用文本接口。" if reuse_text else "视觉/OCR 使用独立接口配置。"
        )

    def save_config(self):
        try:
            AppConfig.save(self.collect_config())
            self.log("info", "已保存 API 与默认策略配置到 pdf2anki_config.json")
        except Exception as e:
            self.log("warning", f"配置保存失败: {e}")

    def check_resume_state(self):
        self.resume_page = None
        self.resume_badge.setText("")
        self.btn_resume.setVisible(False)
        self.btn_start.setText("开始生成卡片")
        self.btn_start.setObjectName("")
        self.btn_start.style().unpolish(self.btn_start)
        self.btn_start.style().polish(self.btn_start)

        if not self.selected_file:
            self.update_summary()
            return

        task_id = f"{os.path.basename(self.selected_file)}_{self.level_combo.currentText()}"
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT current_page FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            conn.close()

            if row and row[0] > 0:
                next_page = row[0] + 1
                if next_page <= self.end_page_spin.value():
                    self.resume_page = next_page
                    self.resume_badge.setText(f"发现本地记录：上次成功处理至第 {row[0]} 页")
                    self.btn_resume.setText(f"从第 {self.resume_page} 页继续生成")
                    self.btn_resume.setVisible(True)
                    self.btn_start.setText("覆盖记录，从头重新开始")
                    self.btn_start.setObjectName("warningButton")
                    self.btn_start.style().unpolish(self.btn_start)
                    self.btn_start.style().polish(self.btn_start)
        except Exception as e:
            pass
        self.update_summary()

    def select_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, '选择PDF', '', 'PDF Files (*.pdf)')
        if fname:
            self.selected_file = fname
            self.file_label.setText(os.path.basename(fname))
            self.check_resume_state()
            self.update_global_status("PDF 已载入，请确认模型配置并开始生成。")

    def update_global_status(self, text):
        self.global_status_label.setText(self.clean_display_text(text))

    def log(self, level, text):
        time_str = datetime.now().strftime('%H:%M:%S')
        if level == "error":
            color = "#fca5a5"
            label = "ERROR"
        elif level == "success":
            color = "#86efac"
            label = "OK"
        elif level == "warning":
            color = "#facc15"
            label = "WARN"
        else:
            color = "#93c5fd"
            label = "INFO"

        safe_text = html.escape(self.clean_display_text(text))
        formatted_text = (
            f"<span style='color: #7d8da5;'>[{time_str}]</span> "
            f"<span style='color: {color}; font-weight: bold;'>[{label}]</span> "
            f"<span style='color: #d5dde8;'>{safe_text}</span>"
        )
        self.log_area.append(formatted_text)
        self.log_area.moveCursor(QTextCursor.MoveOperation.End)

    def update_progress(self, val, text):
        self.progress_bar.setValue(val)
        self.update_summary()
        if text:
            self.status_meta_label.setText(self.clean_display_text(text))

    def update_preview(self, image_bytes, title):
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            self.preview_title_label.setText("图像预览加载失败")
            return
        self.preview_title_label.setText(self.clean_display_text(title))
        target_size = self.preview_image_label.size()
        scaled = pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.preview_image_label.setPixmap(scaled)

    def start_processing(self, resume=False):
        if not self.selected_file:
            self.log("error", "请先选择 PDF 文件")
            self.update_global_status("缺少 PDF 文件，请先选择教材或论文。")
            return
        if not self.text_api_key_input.text().strip():
            self.log("error", "请输入文本模型 API Key")
            self.update_global_status("缺少文本模型 API Key，请补全配置。")
            return
        self.save_config()

        if not resume and self.resume_page is not None:
            task_id = f"{os.path.basename(self.selected_file)}_{self.level_combo.currentText()}"
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
                cursor.execute("DELETE FROM cards WHERE task_id = ?", (task_id,))
                conn.commit()
                conn.close()
                self.log("warning", "已清理该文档的历史缓存数据，重新开始生成。")
                self.resume_page = None
            except Exception as e:
                self.log("error", f"清理缓存失败: {e}")

        self.btn_start.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_select.setEnabled(False)
        self.level_combo.setEnabled(False)
        self.start_page_spin.setEnabled(False)
        self.end_page_spin.setEnabled(False)
        self.update_global_status("任务运行中，正在生成 Anki Cloze 卡片。")
        self.status_meta_label.setText(
            f"范围 {self.start_page_spin.value()}-{self.end_page_spin.value()} 页，模式：{self.level_combo.currentText()}。"
        )
        self.update_summary()

        self.thread = ProcessingThread(
            self.selected_file,
            self.build_api_config(),
            self.start_page_spin.value(),
            self.end_page_spin.value(),
            self.level_combo.currentText(),
            self.resume_page if resume else None
        )
        self.thread.progress_update.connect(self.update_progress)
        self.thread.status_update.connect(self.update_global_status)
        self.thread.log_update.connect(self.log)
        self.thread.preview_update.connect(self.update_preview)
        self.thread.finished_signal.connect(self.on_finished)
        self.thread.start()

    def stop_processing(self):
        if self.thread and self.thread.isRunning():
            self.thread.stop()
            self.btn_start.setEnabled(True)
            self.btn_resume.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.btn_select.setEnabled(True)
            self.level_combo.setEnabled(True)
            self.start_page_spin.setEnabled(True)
            self.end_page_spin.setEnabled(True)
            self.update_global_status("任务已手动暂停，随时可继续。")
            self.check_resume_state()
            self.update_summary()

    def on_finished(self, output_path):
        self.progress_bar.setValue(100)
        self.btn_start.setEnabled(True)
        self.btn_resume.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_select.setEnabled(True)
        self.level_combo.setEnabled(True)
        self.start_page_spin.setEnabled(True)
        self.end_page_spin.setEnabled(True)
        self.update_global_status(f"任务完成，已保存至: {os.path.basename(output_path)}")
        self.log("success", f"任务完成。Anki 包路径: {output_path}")
        self.update_summary()


if __name__ == '__main__':
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        print("Uncaught exception", exc_type, exc_value)
        traceback.print_exception(exc_type, exc_value, exc_traceback)


    sys.excepthook = handle_exception
    app = QApplication(sys.argv)
    ex = MainWindow()
    ex.show()
    sys.exit(app.exec())

