import os
import sys
import re
import json
import time
import random
import getpass
import shutil
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

AUTHOR_LINE = "22级临五yqy制作。启动后输入必要信息即可进入瑞金ai问诊网页自动进行五次问答和病史书写，注意不要点击任何浏览器内容，仅在此框内操作。任何疑问和bug，请联系yang13398@163.com。"
RUN_TIMES = 5
ENV_PATH = ".env"


def get_app_dir():
    """返回程序所在目录。打包成 exe 后，返回 exe 所在目录；直接运行 .py 时，返回 .py 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_app_dir()


def resource_path(relative_path):
    """兼容 PyInstaller 的资源路径。"""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(BASE_DIR, relative_path)


def find_local_browser():
    """
    优先使用本机已安装的 Edge / Chrome，避免携带 Playwright 自带 Chromium。
    如需手动指定浏览器路径，可在 .env 中填写：
    BROWSER_PATH=C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe
    """
    env_path = (os.environ.get("BROWSER_PATH") or "").strip().strip('"')
    if env_path and os.path.exists(env_path):
        return env_path

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    candidates = [
        # Microsoft Edge，Windows 10/11 通常自带
        os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(local_appdata, "Microsoft", "Edge", "Application", "msedge.exe"),

        # Google Chrome
        os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local_appdata, "Google", "Chrome", "Application", "chrome.exe"),
    ]

    # 再尝试 PATH 里的命令
    for cmd in ("msedge", "chrome", "chrome.exe", "msedge.exe"):
        found = shutil.which(cmd)
        if found:
            candidates.append(found)

    for p in candidates:
        if p and os.path.exists(p):
            return p

    return None


def prompt_env_value(name, prompt, secret=False):
    """
    优先读取 .env / 环境变量；若为空，则运行时询问。
    secret=True 时输入内容不会显示在屏幕上。
    """
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if secret:
        return getpass.getpass(prompt).strip()
    return input(prompt).strip()

def ensure_user_env_fields(env_path=ENV_PATH):
    """确保 .env 里有 USER_NAME/STUDENT_NUMBER/USER_EMAIL 三个字段，方便其他用户直接填写。"""
    required = ["USER_NAME", "STUDENT_NUMBER", "USER_EMAIL"]
    existing_keys = set()
    lines = []

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            existing_keys.add(s.split("=", 1)[0].strip())

    missing = [k for k in required if k not in existing_keys]
    if missing:
        with open(env_path, "a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write("\n# 运行者个人信息：请在运行前填写，供提交表单使用\n")
            for k in missing:
                f.write(f"{k}=\n")
        print(f"已在 {env_path} 中补充字段：" + "、".join(missing))
        print("请按需填写 .env 中的 USER_NAME、STUDENT_NUMBER、USER_EMAIL；若留空，程序会在启动时询问。")


print(AUTHOR_LINE)

ensure_user_env_fields()
load_dotenv()

DEEPSEEK_API_KEY = prompt_env_value(
    "DEEPSEEK_API_KEY",
    "请输入 DeepSeek API Key（输入时不会显示；也可提前填入 .env 的 DEEPSEEK_API_KEY=）：",
    secret=True,
)
if not DEEPSEEK_API_KEY:
    raise RuntimeError("DeepSeek API Key 不能为空。")

DEEPSEEK_BASE_URL = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)

# ========== 系统提示词 ==========
CONSULT_PROMPT = """你是一名临床医生，正在对模拟病人问诊，目标是采集完整大病史。

【总规则】
1. 页面已给出的基本信息视为已采集，不要重复询问；后续总结必须使用。
2. 每轮只问一个主题，最多包含2个具体问题。禁止一次性连续询问“姓名、年龄、性别、婚姻、籍贯、住址、职业、科别、床号、住院号”等长串问题。
3. 若患者没有回答当前问题，而是先说症状：先记录症状，下一轮用更短的问题补问漏项；不要把未回答项判为“无”。
4. 患者明确说“不知道/不清楚/不方便说”时，该项记为“未采集”，不纠缠。
5. 不寒暄，不解释，不加前缀，直接输出医生要说的话。
6. 全部问完后只输出：[问诊结束]

【问诊顺序】
一、一般情况：先利用页面已给信息；只补问缺失项。一般情况按小组分轮问：
- 民族、婚姻状况
- 籍贯、现住址
- 职业
- 科别；若为门诊病人，不主动追问病舍、床号、住院号，除非页面或患者提示为住院。

二、主诉与现病史：
- 主要不适是什么，持续多久
- 起病诱因、发展过程
- 加重或缓解因素
- 伴随症状
- 院外就诊、检查和用药情况

三、既往史：
- 既往疾病和传染病史
- 手术外伤史、输血史
- 预防接种史
- 药物/食物过敏史

四、个人史与家族史：
- 吸烟饮酒、疫区/毒物/职业暴露等
- 家族中类似疾病、遗传病、重大慢性病

优先自然推进问诊，但不要为了追求完整而一次问太多。
"""

PHYS_PROMPT = """你是一名临床医生，正在对模拟病人进行体格检查。请根据问诊记录，
向“体格检查助理”逐条说明要做的检查，一次只说一项检查名称，根据返回结果决定下一项。

【必须优先完成的检查】
体格检查开始后，必须先依次完成以下生命体征测量：
1. 体温
2. 脉搏
3. 呼吸
4. 血压

这四项必须逐项单独询问，不要合并成一句“生命体征”或“一般情况”。例如应依次输出：
体温
脉搏
呼吸
血压

完成体温、脉搏、呼吸、血压后，再根据病例选择相关查体项目，例如：
一般情况、皮肤黏膜、浅表淋巴结、头颈部、心脏听诊、肺部听诊、腹部查体、双下肢水肿、神经系统检查等。

【结束规则】
只有在体温、脉搏、呼吸、血压四项都已经得到结果，并且与本病例相关的必要查体也基本完成后，才可以输出：[查体结束]

【容错规则】
若某一项连续两次得到“请告知部位+检查方式(视触叩听)”或类似无法识别的回复，说明助理不支持该项，立即改做明显不同的检查，不要再用近义说法重复同一项。
但体温、脉搏、呼吸、血压四项不能因为“一般情况”已检查就跳过，必须单独尝试询问。

不寒暄、不解释，直接说要查的项目名称。"""

SUMMARY_PROMPT = """根据【页面基本信息】和【问诊对话】整理大病史 JSON。

【优先级】
1. 页面基本信息优先级最高，必须用于一般情况。
2. 问诊中患者明确回答的信息次之。
3. 既往史部分：没问到、患者未回答、患者说不清楚的项，一律填“无”。
4. 除既往史以外：没问到、患者未回答、患者说不清楚的项，一律填“未采集”。
5. 只有患者明确否认某病史或某症状时，才可填“无”；但身份信息、日期信息未采集时禁止填“无”。

【页面基本信息解析规则】
例如：“207号病人，男，40岁，前来门诊问诊，问诊时间：2022年02月22日”
应解析为：
- 姓名：207号病人
- 性别：男
- 年龄：40岁
- 就诊方式：门诊
- 问诊时间：2022年02月22日

【日期格式规则】
- 入院日期、记录日期必须使用 xxxx-xx-xx 格式。
- 若原文为“2016年9月1日”，必须转为“2016-09-01”。
- 若原文为“2022年02月22日”，必须转为“2022-02-22”。
- 入院日期优先使用页面问诊时间；记录日期若无特殊说明，使用程序运行当天日期。

只输出 JSON，不要其他文字：
{
  "一般情况": {
    "姓名": "",
    "年龄": "",
    "性别": "",
    "民族": "",
    "婚姻": "",
    "籍贯": "",
    "住址": "",
    "职业": "",
    "入院日期": "",
    "记录日期": "",
    "科别": "",
    "病舍床号": "",
    "住院号": ""
  },
  "主诉": "",
  "现病史": "",
  "既往史": {
    "疾病史": "",
    "传染病史": "",
    "预防接种史": "",
    "手术外伤史": "",
    "输血史": "",
    "过敏史": ""
  },
  "个人史": "",
  "家族史": ""
}"""

AUX_PROMPT = """你是一名临床医生，需要为这位病人开立辅助检查。下面给你问诊与查体信息，以及"可开立检查项清单"。
请根据病情，选出**确有必要**的检查项——多开或少开都会扣分，请克制、有针对性。
只能从给定清单里选，名称必须与清单完全一致（含括号、标点都要一致）。
只输出一个 JSON 数组，元素为检查项名称，不要任何其他文字。例如：["血常规（CBC）","肝功能常规检查"]"""

DIAGNOSIS_PROMPT = """你是一名严谨的临床医生。请综合下面提供的【问诊大病史】【体格检查】【辅助检查结果】，给出本病例的初步诊断。

要求：
1. 若材料中提供了【本病例标准诊断】，必须以它为准：诊断列表与标准诊断完全一致、一条都不能漏、按其顺序列出。
2. 先逐条梳理所有阳性发现与异常指标（症状、体征、化验/影像/病理异常等）。
3. 给出完整诊断列表，并为每一条附简要诊断依据（对应哪些问诊/查体/辅助检查发现）；若某条标准诊断在现有材料中依据不足，也要列出该诊断并合理推断其依据。
4. 若未提供标准诊断，则自行给出主要诊断+并存/次要诊断+鉴别诊断，并把每项异常落实到某个诊断。

直接输出可填入诊断框的正文，不要使用 ** 等加粗标记，不要寒暄或多余说明。"""

NOTE_PROMPT = """根据以下病例信息，写一段简洁的病历记录小结（150字以内即可），涵盖主诉、关键查体与辅助检查发现、初步诊断思路。直接输出正文，不要使用 ** 等标记。"""

FORM_FILL_PROMPT = """你要把病例信息填入住院病历大病史表单。下面会给你：
1. 页面基本信息
2. 已整理大病史
3. 体格检查
4. 辅助检查结果
5. 标准诊断
6. 系统回顾与查体参考模板
7. 表单字段清单

输出一个 JSON 对象：键=字段名，值=应填写内容。只输出 JSON。

【资料优先级】
标准诊断 > 辅助检查/查体/问诊大病史 > 页面基本信息 > 参考模板。
页面基本信息中的姓名、性别、年龄必须优先使用；若页面已有这些信息，禁止填“无”或“未采集”。

【一般情况字段】
- patientName、gender、age 优先来自页面基本信息。
- ethnicity、marriage、birthplace、occupation、address 若问诊未采集，填“未采集”，不要填“无”。
- admissionDate 优先用页面问诊时间，必须填 xxxx-xx-xx 格式。
- recordDate 使用程序运行当天日期，必须填 xxxx-xx-xx 格式。
- 日期示例：“2016年9月1日”必须写成“2016-09-01”。
- narrator 默认“本人”；reliability 默认“可靠”。

【既往史字段】
- pastHistoryGeneral、pastHistoryDiseases、pastHistoryInfectiousDiseases、pastHistoryVaccination、pastHistorySurgery、pastHistoryTransfusion、pastHistoryDrugAllergy 属于既往史。
- 既往史部分只要未采集、未回答、不清楚，就填“无”，不要填“未采集”。

【非既往史缺失规则】
- 除既往史外，未采集的信息填“未采集”或按参考模板补全阴性描述。
- 身份信息、日期信息未采集时禁止填“无”。

【诊断字段】
- initialDiagnosis：必须以【标准诊断】为准，按原顺序逐条列出，不能漏。每条后写简要依据。
- differentialDiagnosis：至少写3个鉴别诊断，每个说明支持或不支持依据，不能只填“无”。
- treatmentPlan：分条写完整诊疗计划，至少包括进一步检查、一般处理、针对主要诊断治疗、监测指标、随访宣教。

【系统回顾与查体】
- 问诊/查体未采集到的阴性系统回顾和常规查体字段，用参考模板补全。
- 已有阳性发现必须保留，不能被模板阴性描述覆盖。
- 体格检查按视、触、叩、听分别填写到对应字段。

【生命体征】
physicalExamBodyTemperature、physicalExamPulse、physicalExamRespiratoryRate、physicalExamBloodPressureSystolic、physicalExamBloodPressureDiastolic 必填纯数字。
优先用实测值；若无实测，填默认值：36.5、80、18、120、80。

【其他】
- 确实无信息且模板不能覆盖的非既往史字段，填“未采集”或“未见明显异常”。
- 不要使用 ** 加粗标记。
"""

# 系统回顾与查体参考模板（来自用户提供的“系统回顾.docx”，用于补全未采集的阴性描述）
SYSTEM_REVIEW_REF = """【系统回顾】
头颈五官：无头晕、眩晕，无耳鸣、耳痛、听力下降，无鼻塞、流涕、鼻出血，无咽痛、吞咽困难、声音嘶哑，无颈部疼痛、僵硬及包块。
呼吸系统：无慢性咳嗽、咯痰、咯血史，无呼吸困难，无发热、盗汗，无结核患者密切接触史。
循环系统：无心悸、气促、发绀，无心前区疼痛，无晕厥、水肿病史，无动脉硬化，无风湿热病史。
消化系统：无腹痛、腹胀、反酸、嗳气，无呕血、便血，无食欲不振、恶心或呕吐史，大便正常。
泌尿生殖系统：无尿频、尿急、尿痛，无腰痛及排尿困难，无肾毒性药物应用。
造血系统：无苍白、乏力等，皮肤黏膜无瘀点、紫癜，无反复鼻出血或牙龈出血。
内分泌系统及代谢：无畏寒、多汗，无头痛或视力障碍，无食欲异常、烦渴、多尿等，毛发分布均匀，第二性征无改变。
神经精神系统：无头痛、失眠、嗜睡，无喷射性呕吐、记忆力改变，无意识障碍、瘫痪、昏厥、痉挛，无视力障碍、感觉及运动异常，无性格改变。
肌肉骨骼系统：无关节肿痛，无运动障碍，无肢体麻木，无痉挛萎缩或瘫痪史。
精神状态：无焦虑、抑郁、情绪低落，无兴趣减退，无注意力不集中、记忆力下降，无幻觉、妄想，无自杀或自伤观念，无攻击或暴力倾向，自知力完整，社会功能正常。
个人史：生长于原籍，无外地长期居住史，无疫区、疫水接触史，无工业粉尘、毒物、放射性物质接触史，无牧区、矿山、高氟区、低碘区居住史，平日生活规律，否认吸毒史、否认吸烟史，否认饮酒史，否认冶游史。

【查体常规阴性参考】
生命体征：体温36.5℃，脉搏80次/分，呼吸18次/分，血压120/80mmHg（若本病例查体或问诊中另有实测/异常数值，以实测为准）。
一般情况：发育正常，营养良好，神志清楚，自主体位，无慢性病容，表情自如，查体合作。
皮肤黏膜：皮肤弹性正常，皮温可，颜色正常，无皮疹，未见皮下出血、结节、肿块，无蜘蛛痣、肝掌，无瘢痕、溃疡，毛发正常。
淋巴结：全身浅表淋巴结未触及肿大。
头颅：大小、形状正常，无肿块、压痛、瘢痕，头发分布均匀。
眼：眉毛无脱落，眼睑无水肿，上睑无下垂，眼球无突出、运动自如，结膜无充血，巩膜无黄染，角膜透明，双侧瞳孔等大等圆，直径约3mm，对光反射、辐辏反射正常。
耳：耳廓无畸形，外耳道无异常分泌物，乳突无压痛，听力正常。
鼻：无畸形、分泌物，鼻翼无扇动，鼻中隔无偏曲、穿孔，鼻窦无压痛。
口腔：无异味，口唇红润，无龋齿、义齿、残根，牙龈无肿胀，舌居中、运动自如、苔薄白，咽无红肿，扁桃体无肿大，发音清晰。
颈部：对称，活动自如，气管居中，甲状腺未触及肿大，颈静脉无充盈，肝颈静脉回流征阴性，未闻及甲状腺血管杂音。
胸部：胸廓对称，无畸形、压痛，肋间隙正常，呼吸频率约18次/分、节律匀齐，乳房无异常，胸壁无静脉曲张、皮下气肿。
肺：视诊双侧对称、节律规整；触诊呼吸动度一致、无胸膜摩擦感、语颤无增强或减弱；叩诊清音、肺下界正常、移动度6～8cm；听诊双肺呼吸音清晰，无干湿啰音及胸膜摩擦音。
心脏：视诊心前区无隆起，心尖搏动位于胸骨左缘第5肋间锁骨中线内0.5～1.0cm；触诊心尖搏动有力，未触及震颤、抬举样搏动、心包摩擦感；叩诊相对浊音界无明显异常；听诊心律整齐、心音有力，未闻及杂音、附加音及心包摩擦音。
桡动脉：脉搏搏动良好，无奇脉、交替脉。
周围血管征：无毛细血管搏动、枪击音、水冲脉及动脉异常搏动。
腹部：视诊平坦，无肠型、蠕动波、腹壁静脉曲张及局部隆起；触诊腹壁柔软，无压痛、反跳痛、液波震颤及肿块，肝脾肋下未触及，胆囊无压痛、Murphy阴性，输尿管点无压痛；叩诊肝浊音界正常、肝区无叩击痛、无移动性浊音；听诊肠鸣音3次/分，无振水音及血管杂音。
脊柱：正常，活动自如，无压痛及叩击痛。
四肢：无异常形态，无杵状指趾、静脉曲张，肌肉无萎缩，肌张力正常，关节活动自如，无红肿及压痛。
神经系统：生理反射（肱二头肌、肱三头肌、膝腱、跟腱反射）正常；病理反射（巴氏征、奥本汉姆征、戈登征、霍夫曼征）阴性；脑膜刺激征（颈强直、布氏征、克氏征）阴性。"""

FORM_FIELDS = [
    ("patientName","姓名"),("gender","性别"),("age","年龄"),("ethnicity","民族"),
    ("marriage","婚姻"),("birthplace","籍贯"),("occupation","职业"),("address","现住址"),
    ("admissionDate","入院日期"),("recordDate","记录日期"),("narrator","病史陈述者"),("reliability","可靠程度"),
    ("chiefComplaint","主诉"),("presentIllness","现病史"),
    ("pastHistoryGeneral","既往一般健康状况"),("pastHistoryDiseases","既往疾病史"),
    ("pastHistoryInfectiousDiseases","传染病史"),("pastHistoryVaccination","预防接种史"),
    ("pastHistorySurgery","手术外伤史"),("pastHistoryTransfusion","输血史"),("pastHistoryDrugAllergy","药物过敏史"),
    ("systemReviewOtorhinolaryngology","系统回顾-头颈五官"),("systemReviewRespiratory","系统回顾-呼吸"),
    ("systemReviewCirculatory","系统回顾-循环"),("systemReviewDigestive","系统回顾-消化"),
    ("systemReviewUrinary","系统回顾-泌尿"),("systemReviewEndocrine","系统回顾-内分泌代谢"),
    ("systemReviewHematopoietic","系统回顾-造血"),("systemReviewMusculoskeletal","系统回顾-肌肉骨关节"),
    ("systemReviewNervous","系统回顾-神经"),("systemReviewMentalState","系统回顾-精神状态"),
    ("personalHistory","个人史"),("marriageHistory","婚姻史"),("fertilityHistory","生育史"),
    ("menstrualHistory","月经史"),("familyHistory","家族史"),
    ("physicalExamBodyTemperature","体温(℃)"),("physicalExamPulse","脉搏(次/分)"),
    ("physicalExamRespiratoryRate","呼吸(次/分)"),("physicalExamBloodPressureSystolic","收缩压"),
    ("physicalExamBloodPressureDiastolic","舒张压(mmHg)"),
    ("physicalExamGeneral","查体-一般情况"),("physicalExamDermatology","查体-皮肤黏膜"),
    ("physicalExamLymph","查体-淋巴结"),("physicalExamHead","查体-头部"),("physicalExamEyes","查体-眼"),
    ("physicalExamEars","查体-耳"),("physicalExamNose","查体-鼻"),("physicalExamOral","查体-口腔"),
    ("physicalExamCervical","查体-颈部"),("physicalExamThorax","查体-胸部"),
    ("lungsInspection","肺-视诊"),("lungsPalpation","肺-触诊"),("lungsPercussion","肺-叩诊"),("lungsAuscultation","肺-听诊"),
    ("cardiacInspection","心-视诊"),("cardiacPalpation","心-触诊"),("cardiacPercussion","心-叩诊"),("cardiacAuscultation","心-听诊"),
    ("radialArtery","桡动脉"),("peripheralVesselSigns","周围血管征"),
    ("abdomenInspection","腹-视诊"),("abdomenPalpation","腹-触诊"),("abdomenPercussion","腹-叩诊"),("abdomenAuscultation","腹-听诊"),
    ("rectumAndAnus","直肠与肛门"),("pudendum","外生殖器"),("spine","脊柱"),("limbs","四肢"),
    ("nervousSystem","查体-神经系统"),("specializedExamination","专科检查"),
    ("laboratoryExamination","实验室及特殊检查"),("initialDiagnosis","初步诊断及诊断依据"),
    ("differentialDiagnosis","鉴别诊断"),("treatmentPlan","诊疗计划"),
]

# ========== 辅助检查菜单（三级树）==========
AUX_TREE = {
  "实验室检查": {
    "血液学检查": {"血常规（CBC）": "x", "凝血四项": "x", "红细胞沉降率": "x", "D二聚体": "x", "纤维蛋白(原)降解产物测定(FDP)": "x"},
    "尿液检查": {"尿常规": "x", "24小时尿量": "x", "24小时尿蛋白定量": "x"},
    "粪便检查": {"粪便常规": "x", "粪隐血实验": "x"},
    "体液与分泌物检查": {"呕吐物潜血试验": "x"},
    "生化检查": {"肝功能常规检查": "x", "肾功能常规检查": "x", "电解质检查": "x", "淀粉酶": "x", "血脂常规检查": "x", "血气分析": "x", "心肌酶谱常规检查": "x", "空腹血糖测定": "x", "随机血糖测定": "x", "糖化血红蛋白": "x", "葡萄糖耐量试验(OGTT)": "x", "胰岛素释放试验": "x", "血浆氨测定": "x", "前脑钠肽": "x", "血清肌钙蛋白测定": "x"},
    "免疫学检查": {"乙肝三系检查": "x", "肿瘤标志物检测": "x", "免疫球蛋白定量测定": "x", "C反应蛋白": "x"},
  },
  "影像检查": {
    "X线": {"胸部X线": "x"},
    "CT": {"颅脑CT": "x", "胸部CT": "x", "腹部CT": "x"},
    "超声": {"超声心动图": "x", "腹部:肝胆胰脾超声": "x", "泌尿系B超:肾脏\\输尿管\\膀胱": "x"},
  },
  "心电图": {"常规心电图": "x"},
  "内镜检查": {"支气管镜检查": "x", "胃镜检查": "x", "结肠镜检查": "x"},
  "病理检查": {"淋巴结活检": "x", "骨髓活检": "x"},
}


def build_leaf_paths(tree, prefix=()):
    """返回 {叶子名: [完整路径...]}。叶子=值不是 dict 的项。"""
    out = {}
    for k, v in tree.items():
        if isinstance(v, dict):
            out.update(build_leaf_paths(v, prefix + (k,)))
        else:
            out[k] = list(prefix) + [k]
    return out


LEAF_PATHS = build_leaf_paths(AUX_TREE)   # 叶子名 -> 路径


def ds_reply(system_prompt, history):
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "system", "content": system_prompt}, *history],
    )
    return resp.choices[0].message.content.strip()


def ds_once(system_prompt, user_content):
    """单轮调用 DeepSeek。"""
    return client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_content}],
    ).choices[0].message.content.strip()


def get_user_info():
    """读取运行者个人信息：优先 .env；若 .env 留空，则运行时询问。"""
    name = (os.environ.get("USER_NAME") or "").strip()
    sid = (os.environ.get("STUDENT_NUMBER") or "").strip()
    email = (os.environ.get("USER_EMAIL") or "").strip()

    if not name:
        name = input("请输入姓名（也可下次提前填入 .env 的 USER_NAME=）：").strip()
    if not sid:
        sid = input("请输入学号/工号（也可下次提前填入 .env 的 STUDENT_NUMBER=）：").strip()
    if not email:
        email = input("请输入邮箱（也可下次提前填入 .env 的 USER_EMAIL=）：").strip()

    print("\n将用以下信息填写表单：")
    print(f"  姓名：{name}")
    print(f"  学号/工号：{sid}")
    print(f"  邮箱：{email}")
    return name, sid, email


def get_site_credentials():
    """读取网站登录账号密码：优先 .env；若 .env 留空，则运行时询问。"""
    site_user = prompt_env_value(
        "SITE_USER",
        "请输入网站账号（也可提前填入 .env 的 SITE_USER=）：",
        secret=False,
    )
    site_pass = prompt_env_value(
        "SITE_PASS",
        "请输入网站密码（输入时不会显示；也可提前填入 .env 的 SITE_PASS=）：",
        secret=True,
    )
    if not site_user or not site_pass:
        raise RuntimeError("网站账号和密码不能为空。")
    return site_user, site_pass


# ========== 通用页面操作 ==========
def consult_region(page):
    return page.get_by_text("问诊对话区域", exact=True).locator("xpath=..")


def phys_region(page):
    return page.get_by_role("tabpanel").filter(has_text="体格检查助理")


def aux_region(page):
    return page.get_by_role("tabpanel").filter(has_text="辅助检查助理")


def latest_reply(region):
    return region.locator(".self-start .p-card-content").last.inner_text().strip()


def send_in(region, text):
    box = region.locator("textarea")
    box.click()
    box.fill(text)
    box.press("Enter")


def wait_new(region, prev_count, timeout=60):
    msgs = region.locator(".self-start .p-card-content")
    deadline = time.time() + timeout
    while msgs.count() <= prev_count and time.time() < deadline:
        time.sleep(0.3)
    last, stable = None, 0
    while stable < 5 and time.time() < deadline:
        cur = msgs.last.inner_text().strip()
        stable = stable + 1 if (cur and cur == last) else 0
        last = cur
        time.sleep(0.3)


def switch_tab(page, name):
    page.get_by_role("tab", name=name, exact=True).click()
    time.sleep(1)


def open_system_dropdown(page):
    """打开系统下拉；返回当前 options locator。"""
    box = page.locator(".p-select").filter(has_text="请选择").first
    box.wait_for(state="visible", timeout=15000)
    options = page.get_by_role("option")

    clickers = [lambda: box.locator(".p-select-dropdown").click(),  # 点下拉箭头
                lambda: box.click()]                                # 点整个框
    for i in range(8):
        if options.count() > 0 and options.first.is_visible():      # 已经开着就别再点
            break
        try:
            clickers[i % 2]()                                       # 交替两种点法
        except Exception:
            pass
        try:
            options.first.wait_for(state="visible", timeout=2000)  # 开了就立刻退出
            break
        except Exception:
            time.sleep(0.4)

    options.first.wait_for(state="visible", timeout=8000)
    return options


def select_system(page):
    """首次打开“系统”下拉、列出系统、按编号选择；返回系统名称。"""
    options = open_system_dropdown(page)
    names = [t.strip() for t in options.all_inner_texts()]
    print("\n可选系统：")
    for i, n in enumerate(names):
        print(f"  {i + 1}. {n}")
    while True:
        sel = input("输入要选择的系统编号：").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(names):
            break
        print("编号无效，请重输。")
    chosen = names[int(sel) - 1]
    options.nth(int(sel) - 1).click()
    time.sleep(0.8)
    print(f"已选择：{chosen}")
    return chosen


def select_system_by_name(page, system_name):
    """后续自动重复时，按首次选择的系统名称自动选择。"""
    options = open_system_dropdown(page)
    names = [t.strip() for t in options.all_inner_texts()]
    if system_name in names:
        idx = names.index(system_name)
    else:
        # 名称轻微变化时做包含匹配兜底
        idx = None
        for i, n in enumerate(names):
            if system_name and (system_name in n or n in system_name):
                idx = i
                break
        if idx is None:
            raise RuntimeError(f"未找到上次选择的系统：{system_name}；当前可选：{names}")
    options.nth(idx).click()
    time.sleep(0.8)
    print(f"已自动选择：{names[idx]}")
    return names[idx]


def enter_practice_mode(page):
    """进入系统练习模式。"""
    page.goto("https://vsp.rjh.com.cn:8080", wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("heading", name="系统练习模式").click()
    page.wait_for_url("**/practice", timeout=30000)


def wait_case_ready(page, timeout=60000):
    """等待病例/问诊界面加载完成。"""
    page.get_by_text("问诊时间").first.wait_for(state="visible", timeout=timeout)
    consult_region(page).locator("textarea").wait_for(state="visible", timeout=timeout)
    time.sleep(1)


def start_case_with_system(page, system_name=None):
    """进入练习模式并选择系统；system_name 为空时手动选择，否则自动选择。"""
    enter_practice_mode(page)
    if system_name:
        selected = select_system_by_name(page, system_name)
    else:
        selected = select_system(page)
    wait_case_ready(page)
    return selected

def get_consult_info(page):
    text = page.get_by_text("问诊时间").first.inner_text().strip()
    m = re.search(r"问诊时间[：:]\s*([\d年月日./\-]+)", text)
    return text, (m.group(1) if m else "未采集")


UNKNOWN_VALUES = {"", "无", "未采集", "不详", "未知", "None", "none", "null", "NULL"}


def normalize_date_string(text, default=""):
    """
    把常见日期格式统一为 YYYY-MM-DD。
    支持：2022年02月22日、2016年9月1日、2022/2/22、2022.02.22、2022-2-22。
    无法解析时返回 default。
    """
    if not text:
        return default
    s = str(text).strip()
    if not s or s in UNKNOWN_VALUES:
        return default

    # 先从长文本中找日期，避免传入整句“问诊时间：2022年02月22日”时失败
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if not m:
        m = re.search(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})", s)
    if not m:
        return default

    y, mo, d = m.groups()
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return default


def extract_basic_from_header(text):
    """
    从页面页头提取基本信息。
    兼容示例：207号病人，男，40岁，前来门诊问诊  问诊时间：2022年02月22日
    返回：{"姓名":"207号病人", "性别":"男", "年龄":"40岁", "入院日期":"2022-02-22"}
    """
    info = {}
    if not text:
        return info

    compact = re.sub(r"\s+", " ", text).strip()

    # 优先匹配“207号病人，男，40岁”这类格式
    m = re.search(r"([^，,\s]+号病人)\s*[，,]\s*(男|女)\s*[，,]\s*(\d{1,3})\s*岁", compact)
    if m:
        info["姓名"] = m.group(1).strip()
        info["性别"] = m.group(2).strip()
        info["年龄"] = m.group(3).strip() + "岁"
    else:
        # 兜底：找“男/女”和“xx岁”
        gender = re.search(r"(?<![一-龥])(男|女)(?![一-龥])", compact)
        age = re.search(r"(\d{1,3})\s*岁", compact)
        name = re.search(r"([^，,\s]+号病人)", compact)
        if name:
            info["姓名"] = name.group(1).strip()
        if gender:
            info["性别"] = gender.group(1)
        if age:
            info["年龄"] = age.group(1) + "岁"

    dt = normalize_date_string(compact, default="")
    if dt:
        # 该系统里“问诊时间”通常就是病历书写要求中的入院/就诊日期
        info["入院日期"] = dt

    return info


def is_unknown_value(value):
    return str(value).strip() in UNKNOWN_VALUES


def normalize_dabingshi_missing_values(dabingshi, patient_info=""):
    """
    统一大病史缺失值规则：
    - 既往史：未采集/空/不详 -> 无
    - 非既往史：未采集/空/不详 -> 未采集
    - 一般情况日期：统一 YYYY-MM-DD
    """
    if not isinstance(dabingshi, dict):
        return dabingshi

    # 一般情况
    dabingshi.setdefault("一般情况", {})
    if not isinstance(dabingshi["一般情况"], dict):
        dabingshi["一般情况"] = {}
    general = dabingshi["一般情况"]

    # 日期：入院日期优先从页面问诊时间取；记录日期用程序当天
    basic = extract_basic_from_header(patient_info)
    today = time.strftime("%Y-%m-%d")
    admission_date = normalize_date_string(general.get("入院日期", ""), default="") or basic.get("入院日期", "")
    if admission_date:
        general["入院日期"] = admission_date
    elif is_unknown_value(general.get("入院日期", "")):
        general["入院日期"] = "未采集"

    record_date = normalize_date_string(general.get("记录日期", ""), default="") or today
    general["记录日期"] = record_date

    # 一般情况未采集不能填“无”
    identity_keys = ["姓名", "年龄", "性别", "民族", "婚姻", "籍贯", "住址", "职业", "科别", "病舍床号", "住院号", "入院日期", "记录日期"]
    for k in identity_keys:
        old = str(general.get(k, "")).strip()
        if old == "" or old == "无" or old in {"不详", "未知", "None", "none", "null", "NULL"}:
            general[k] = "未采集"

    # 既往史未采集统一填“无”
    dabingshi.setdefault("既往史", {})
    if not isinstance(dabingshi["既往史"], dict):
        dabingshi["既往史"] = {}
    past_keys = ["疾病史", "传染病史", "预防接种史", "手术外伤史", "输血史", "过敏史"]
    for k in past_keys:
        if is_unknown_value(dabingshi["既往史"].get(k, "")):
            dabingshi["既往史"][k] = "无"

    # 非既往史的顶层字段未采集填“未采集”
    for k in ["主诉", "现病史", "个人史", "家族史"]:
        if is_unknown_value(dabingshi.get(k, "")):
            dabingshi[k] = "未采集"

    return dabingshi


def backfill_basic_info(dabingshi, patient_info):
    """
    用页面基本信息硬兜底大病史的一般情况，防止模型把姓名/性别/年龄写成“无”。
    同时执行缺失值和日期格式规则。
    """
    if not isinstance(dabingshi, dict):
        return dabingshi
    dabingshi.setdefault("一般情况", {})
    if not isinstance(dabingshi["一般情况"], dict):
        dabingshi["一般情况"] = {}

    basic = extract_basic_from_header(patient_info)
    for k, v in basic.items():
        if v:
            old = dabingshi["一般情况"].get(k, "")
            if is_unknown_value(old):
                dabingshi["一般情况"][k] = v

    dabingshi = normalize_dabingshi_missing_values(dabingshi, patient_info)

    # 页面已有姓名/性别/年龄/入院日期时，最终强制覆盖，避免被“未采集”覆盖
    for k, v in basic.items():
        if v:
            dabingshi["一般情况"][k] = v

    # 记录日期永远用 YYYY-MM-DD
    dabingshi["一般情况"]["记录日期"] = time.strftime("%Y-%m-%d")
    return dabingshi


def enforce_past_history_form_defaults(filled):
    """表单既往史字段：未采集/空/不详统一填“无”。"""
    if not isinstance(filled, dict):
        return filled
    past_fields = [
        "pastHistoryGeneral",
        "pastHistoryDiseases",
        "pastHistoryInfectiousDiseases",
        "pastHistoryVaccination",
        "pastHistorySurgery",
        "pastHistoryTransfusion",
        "pastHistoryDrugAllergy",
    ]
    for field in past_fields:
        if is_unknown_value(filled.get(field, "")):
            filled[field] = "无"
    return filled


def backfill_form_identity_fields(filled, info_text):
    """
    填表前再次兜底身份字段。页面基本信息里的姓名/性别/年龄强制覆盖；
    其他身份字段若模型填“无”，改为“未采集”。日期统一 YYYY-MM-DD。
    """
    if not isinstance(filled, dict):
        return filled

    basic = extract_basic_from_header(info_text)
    mapping = {"姓名": "patientName", "性别": "gender", "年龄": "age"}
    for zh, field in mapping.items():
        if basic.get(zh):
            filled[field] = basic[zh]

    identity_fields = ["ethnicity", "marriage", "birthplace", "occupation", "address"]
    for field in identity_fields:
        if str(filled.get(field, "")).strip() in {"", "无", "不详", "未知", "None", "none", "null", "NULL"}:
            filled[field] = "未采集"

    # 日期字段强制标准化为 YYYY-MM-DD
    today = time.strftime("%Y-%m-%d")
    filled["admissionDate"] = normalize_date_string(filled.get("admissionDate", ""), default="") or basic.get("入院日期", "") or today
    filled["recordDate"] = normalize_date_string(filled.get("recordDate", ""), default="") or today

    return filled


def normalize_marker(text):
    """统一括号格式"""
    return (
        text.replace("【", "[")
            .replace("】", "]")
            .replace("（", "(")
            .replace("）", ")")
            .strip()
    )


def reached_end(reply, end_marker):
    """
    判断是否出现结束标记
    支持：
    [查体结束]
    【查体结束】
    [问诊结束]
    【问诊结束】
    """
    txt = normalize_marker(reply)
    marker = normalize_marker(end_marker)
    return marker in txt


def run_chat(region, system_prompt, end_marker, max_turns=60):
    history = []
    prev_msg, stuck = None, 0
    for turn in range(max_turns):
        msg = latest_reply(region)
        print(f"  [对方] {msg}")
        history.append({"role": "user", "content": msg})

        if ("体格检查信息已记录完毕" in msg or "问诊信息已记录完毕" in msg):
            print("  （页面已结束）")
            break

        # 卡住检测：对方连续重复同一句（多半是没听懂、反复要你换说法）
        stuck = stuck + 1 if msg == prev_msg else 0
        prev_msg = msg
        if stuck >= 4:
            print("  （反复卡在同一项，强制结束本阶段）")
            break
        if stuck >= 2:
            history.append({"role": "user", "content":
                "[系统提示] 助理已多次表示无法识别你刚才的检查/说法。不要再重复同一项或它的近义说法；"
                "改做一个明显不同的检查项目；若关键查体已基本完成，直接输出" + end_marker + "。"})

        reply = ds_reply(system_prompt, history)
        print(f"  [医生] {reply}")
        history.append({"role": "assistant", "content": reply})

        if reached_end(reply, end_marker):
            print(f"  （{end_marker}）")
            break

        cnt = region.locator(".self-start .p-card-content").count()
        send_in(region, reply)
        wait_new(region, cnt)
        time.sleep(random.uniform(2, 5))
    else:
        print(f"  [警告] 达到最大轮数 {max_turns}，强制退出")
    return history


# ========== 辅助检查：级联选择（带容错与重试）==========
def dropdown_is_open(page):
    top = page.get_by_role("treeitem", name="实验室检查", exact=True)
    return top.count() > 0 and top.first.is_visible()


def ensure_open(page):
    if not dropdown_is_open(page):
        aux_region(page).locator(".p-cascadeselect-dropdown").first.click()
        time.sleep(0.5)


def reveal_child(page, group_name, child_name):
    """展开 group_name 让 child_name 出现：宽屏靠悬停，窄屏靠点击，两种都兼容。"""
    group = page.get_by_role("treeitem", name=group_name, exact=True)
    group.wait_for(state="visible", timeout=8000)
    content = group.locator(".p-cascadeselect-option-content").first
    child = page.get_by_role("treeitem", name=child_name, exact=True)
    content.hover()                       # 宽屏：悬停即弹出子菜单
    try:
        child.wait_for(state="visible", timeout=2500)
    except Exception:
        content.click()                   # 窄屏/移动模式：改用点击
        child.wait_for(state="visible", timeout=5000)


def select_one_test(page, path, attempts=2):
    """按路径展开并选中一个检查（多选会累积），不提交；失败自动重试。"""
    for i in range(attempts):
        try:
            ensure_open(page)
            for d in range(len(path) - 1):       # path[d] 展开出 path[d+1]
                reveal_child(page, path[d], path[d + 1])
            leaf = page.get_by_role("treeitem", name=path[-1], exact=True)
            leaf.wait_for(state="visible", timeout=8000)
            leaf.locator(".p-cascadeselect-option-content").first.click()  # 选中叶子
            time.sleep(0.4)
            return True
        except Exception as e:
            print(f"  [重试 {i + 1}/{attempts}] {path[-1]}：{e}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.8)
    print(f"  [失败] 未能选中：{path[-1]}")
    return False


def grab_aux_results(page, timeout=120):
    """提交后等待并读取辅助检查结果文本（结果可能分批出现）。"""
    def collect():
        texts = aux_region(page).locator(".p-card-content").all_inner_texts()
        return [t.strip() for t in texts if ("结果如下" in t or "结果已获取" in t)]

    deadline = time.time() + timeout
    while time.time() < deadline:
        got = collect()
        if got:
            time.sleep(3)                 # 再等一下，后续结果可能继续返回
            return "\n\n".join(collect())
        time.sleep(1)
    return ""


def grab_answer_diagnosis(page, timeout=30):
    """提交辅助检查后弹窗里显示本病例标准诊断（答案），抓取纯文本。"""
    loc = page.locator("div.whitespace-pre-wrap.pl-8.font-semibold")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for i in range(loc.count()):
                txt = loc.nth(i).inner_text().strip()
                if txt:
                    return txt
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def enter_history_writing(page):
    """点“病史书写”进入大病史书写环节（同时关掉答案弹窗）。"""
    btn = page.get_by_role("button", name="病史书写", exact=True)
    btn.wait_for(state="visible", timeout=15000)
    btn.click()
    time.sleep(1)


def fill_notepad(page, text):
    """提交前必须填记事本：点“记事本” -> 弹窗“确定” -> 在编辑器输入内容。"""
    page.get_by_role("button", name="记事本", exact=True).click()
    time.sleep(0.6)
    page.get_by_role("button", name="确定", exact=True).click()
    time.sleep(0.6)
    editor = page.locator(".ql-editor").first
    editor.click()
    editor.fill(text)
    time.sleep(0.4)


def fill_history_form(page, info_text, uname, sid, consult_date=""):
    fields_desc = "\n".join(f"{n}：{d}" for n, d in FORM_FIELDS)
    raw = ds_once(
        FORM_FILL_PROMPT,
        info_text
        + "\n\n【系统回顾与查体参考模板】\n" + SYSTEM_REVIEW_REF
        + "\n\n【表单字段（字段名：含义）】\n" + fields_desc,
    )
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        filled = json.loads(raw)
    except json.JSONDecodeError:
        print("表单字段返回非合法 JSON，原文前 500 字：\n", raw[:500])
        return {}

    # 身份字段兜底：页面基本信息里的姓名/性别/年龄强制覆盖；
    # 民族、婚姻、籍贯、职业、现住址等未采集项不允许填“无”。
    # 日期字段统一 YYYY-MM-DD。
    filled = backfill_form_identity_fields(filled, info_text)

    # 既往史字段兜底：未采集/空/不详统一填“无”。
    filled = enforce_past_history_form_defaults(filled)

    # 生命体征兜底：DeepSeek 若漏填，用默认值补上，确保表单里这几项一定有数值
    vital_defaults = {
        "physicalExamBodyTemperature": "36.5",
        "physicalExamPulse": "80",
        "physicalExamRespiratoryRate": "18",
        "physicalExamBloodPressureSystolic": "120",
        "physicalExamBloodPressureDiastolic": "80",
    }
    for k, dv in vital_defaults.items():
        if not str(filled.get(k, "")).strip():
            filled[k] = dv

    # 固定/规则填写项：直接覆盖，不交给 DeepSeek 判断
    today = time.strftime("%Y-%m-%d")               # 电脑系统日期，格式 YYYY-MM-DD
    normalized_consult_date = normalize_date_string(consult_date, default="")
    filled["admissionDate"] = normalized_consult_date or normalize_date_string(filled.get("admissionDate", ""), default="") or today  # 入院日期=问诊时间
    filled["recordDate"] = today                    # 记录日期=系统当天，格式 YYYY-MM-DD
    filled["narrator"] = filled.get("narrator", "") or "本人"   # 病史陈述者，无特殊说明=本人
    filled["reliability"] = "可靠"                  # 可靠程度固定
    filled["department"] = "原神科"                  # 科别（采集不到，固定填）
    filled["wardBed"] = "8床"                        # 病舍床号（采集不到，固定填）
    filled["hospitalId"] = "000024"                  # 住院号（采集不到，固定填）
    for k in ("rectumAndAnus", "pudendum"):         # 直肠肛门 / 外生殖器 无信息=未检
        if not str(filled.get(k, "")).strip():
            filled[k] = "未检"

    for name, value in filled.items():  # 按 name 填，重名的全填
        if not value:
            continue
        loc = page.locator(f'[name="{name}"]')
        for i in range(loc.count()):
            try:
                loc.nth(i).fill(str(value).replace("**", ""))
            except Exception as e:
                print(f"  [跳过] {name}: {e}")
    for nm, val in [("recorder", uname), ("studentName", uname), ("studentId", sid)]:
        loc = page.locator(f'[name="{nm}"]')  # 记录人/学生本人信息直接填 env 里的姓名
        if loc.count():
            try:
                loc.first.fill(val)
            except Exception:
                pass
    return filled


# ========== 主流程 ==========
def save_case_json(case_no, result):
    """同时保存“最新结果”和“第N例结果”，避免五次循环互相覆盖。"""
    latest_path = os.path.join(BASE_DIR, "大病史.json")
    case_path = os.path.join(BASE_DIR, f"大病史_第{case_no}例.json")
    for path in (latest_path, case_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


def save_phys_json(case_no, phys_history):
    latest_path = os.path.join(BASE_DIR, "体格检查.json")
    case_path = os.path.join(BASE_DIR, f"体格检查_第{case_no}例.json")
    for path in (latest_path, case_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(phys_history, f, ensure_ascii=False, indent=2)


def submit_history_form(page):
    """自动提交大病史表单；按钮名称因页面版本可能不同，逐个尝试。"""
    page.keyboard.press("Escape")
    time.sleep(0.5)
    button_names = ["提交", "总提交", "提交病史", "提交大病史", "完成", "保存"]
    for name in button_names:
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count() > 0:
                btn.last.click(timeout=5000)
                print(f"已点击大病史表单按钮：{name}")
                time.sleep(2)
                return True
        except Exception:
            pass
    print("未找到大病史表单提交按钮，已跳过自动提交。")
    return False


def run_one_case(page, case_no, user_name, student_number, user_email):
    print(f"\n========== 第 {case_no} / {RUN_TIMES} 例开始 ==========")

    patient_info, consult_date = get_consult_info(page)
    consult_date_std = normalize_date_string(consult_date, default=consult_date)
    print(f"[基本信息] {patient_info}")

    # ===== 阶段1：问诊 =====
    print("\n===== 阶段1 问诊 =====")
    consult_system = (
        CONSULT_PROMPT
        + f"\n\n【页面基本信息】\n{patient_info}"
        + f"\n【问诊时间】{consult_date}"
    )
    consult_history = run_chat(consult_region(page), consult_system, "[问诊结束]")

    summary_input = (
        f"【页面基本信息】\n{patient_info}\n"
        f"【问诊时间】{consult_date}\n\n"
        + SUMMARY_PROMPT
    )
    summary = client.chat.completions.create(
        model="deepseek-chat",
        messages=[*consult_history, {"role": "user", "content": summary_input}],
    ).choices[0].message.content
    summary = summary.replace("```json", "").replace("```", "").strip()
    try:
        dabingshi = json.loads(summary)
        dabingshi = backfill_basic_info(dabingshi, patient_info)
    except json.JSONDecodeError:
        dabingshi = {"原始文本": summary}
    result = {"问诊时间": consult_date_std, "病人基本信息": patient_info, "大病史": dabingshi}
    save_case_json(case_no, result)
    print(f"已保存 大病史_第{case_no}例.json")

    # ===== 阶段2：体格检查 =====
    print("\n===== 阶段2 体格检查 =====")
    switch_tab(page, "体格检查")
    phys_system = PHYS_PROMPT + "\n\n【问诊记录，供参考】\n" + json.dumps(dabingshi, ensure_ascii=False)
    phys_history = run_chat(phys_region(page), phys_system, "[查体结束]", max_turns=30)
    save_phys_json(case_no, phys_history)
    print(f"已保存 体格检查_第{case_no}例.json")

    # ===== 阶段3：辅助检查（全自动）=====
    print("\n===== 阶段3 辅助检查 =====")
    switch_tab(page, "辅助检查")

    menu = list(LEAF_PATHS.keys())
    aux_input = (
        "【问诊大病史】\n" + json.dumps(dabingshi, ensure_ascii=False)
        + "\n\n【体格检查记录】\n" + json.dumps(phys_history, ensure_ascii=False)
        + "\n\n【可开立检查项清单】\n" + json.dumps(menu, ensure_ascii=False)
    )
    raw = ds_once(AUX_PROMPT, aux_input)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        chosen = json.loads(raw)
    except json.JSONDecodeError:
        chosen = []
    valid = [c for c in chosen if c in LEAF_PATHS]
    invalid = [c for c in chosen if c not in LEAF_PATHS]

    print("\nDeepSeek 建议开立以下检查：")
    for c in valid:
        print("  -", " › ".join(LEAF_PATHS[c]))
    if invalid:
        print("  （以下不在清单中，已忽略：", invalid, "）")

    for c in valid:
        print("选中：", c)
        select_one_test(page, LEAF_PATHS[c])

    page.keyboard.press("Escape")
    time.sleep(0.3)
    aux_region(page).get_by_role("button", name="提交辅助检查", exact=True).click()
    print("已提交辅助检查，等待结果返回...")

    aux_results = grab_aux_results(page)
    print("\n辅助检查结果：\n", aux_results if aux_results else "(未读到结果，请在界面查看)")

    # ===== 诊断框：此时还没有标准答案，由 DeepSeek 自行判断 =====
    diag_input = (
        "【页面基本信息】\n" + patient_info
        + "\n\n【大病史】\n" + json.dumps(dabingshi, ensure_ascii=False)
        + "\n\n【体格检查】\n" + json.dumps(phys_history, ensure_ascii=False)
        + "\n\n【辅助检查结果】\n" + aux_results
    )
    diagnosis = ds_once(DIAGNOSIS_PROMPT, diag_input).replace("**", "")
    print("\n初步诊断：\n", diagnosis)

    note_text = ds_once(NOTE_PROMPT, diag_input + "\n\n【初步诊断】\n" + diagnosis).replace("**", "")

    diag_box = page.locator('textarea[name="conclusion"]')
    diag_box.click()
    diag_box.fill(diagnosis)
    print("已把初步诊断填入诊断框。")

    page.locator('input[name="name"]').fill(user_name)
    page.locator('input[name="studentNumber"]').fill(student_number)
    page.locator('input[name="email"]').fill(user_email)
    print("已填写姓名 / 学号 / 邮箱。")

    fill_notepad(page, note_text)
    print("已在记事本填写病历记录。")

    page.keyboard.press("Escape")
    time.sleep(0.3)
    page.get_by_role("button", name="提交", exact=True).click()
    print("已点击总提交，等待标准诊断答案弹窗...")

    answer_dx = grab_answer_diagnosis(page)
    print("\n系统标准诊断（答案）：\n", answer_dx if answer_dx else "(未抓到答案)")

    result["标准诊断"] = answer_dx
    result["辅助检查结果"] = aux_results
    result["初步诊断"] = diagnosis
    result["记事本"] = note_text
    save_case_json(case_no, result)
    print(f"已更新 大病史_第{case_no}例.json（含标准诊断、辅助检查结果、初步诊断、记事本）")

    # ===== 阶段4：点“病史书写”进入大病史表单 =====
    enter_history_writing(page)
    try:
        page.wait_for_url("**/history-taking**", timeout=60000)
    except Exception:
        print("（没检测到 history-taking 链接，仍按字段名尝试填写）")

    info_text = (
        "【页面基本信息】\n" + patient_info
        + "\n\n【大病史】\n" + json.dumps(dabingshi, ensure_ascii=False)
        + "\n\n【体格检查】\n" + json.dumps(phys_history, ensure_ascii=False)
        + "\n\n【辅助检查结果】\n" + aux_results
        + "\n\n【初步诊断】\n" + diagnosis
        + f"\n\n【问诊时间】{consult_date}"
    )
    if answer_dx:
        info_text += "\n\n【本病例标准诊断（以此为准）】\n" + answer_dx

    filled = fill_history_form(page, info_text, user_name, student_number, consult_date)
    result["大病史表单"] = filled
    save_case_json(case_no, result)
    print(f"\n第 {case_no} 例大病史表单已填好。")

    submit_history_form(page)
    print(f"========== 第 {case_no} / {RUN_TIMES} 例结束 ==========\n")
    return result


def main():
    user_name, student_number, user_email = get_user_info()
    site_user, site_pass = get_site_credentials()

    with sync_playwright() as p:
        browser_path = find_local_browser()
        if not browser_path:
            raise RuntimeError(
                "未找到本机 Edge 或 Chrome 浏览器。\n"
                "请安装 Microsoft Edge 或 Google Chrome 后再运行；\n"
                "或在 .env 中手动填写 BROWSER_PATH=浏览器exe完整路径。"
            )

        print(f"将使用本机浏览器：{browser_path}")

        ctx = p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "userdata"),
            executable_path=browser_path,
            headless=False,
            http_credentials={
                "username": site_user,
                "password": site_pass,
            },
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        selected_system = start_case_with_system(page, system_name=None)

        all_results = []
        for case_no in range(1, RUN_TIMES + 1):
            if case_no > 1:
                selected_system = start_case_with_system(page, system_name=selected_system)
            try:
                result = run_one_case(page, case_no, user_name, student_number, user_email)
                all_results.append(result)
            except Exception as e:
                print(f"第 {case_no} 例运行失败：{e}")
                break

        with open(os.path.join(BASE_DIR, "五次练习汇总.json"), "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"已保存 五次练习汇总.json；本次共完成 {len(all_results)} / {RUN_TIMES} 例。")
        input("\n全部流程结束，按回车关闭浏览器...")
        ctx.close()


if __name__ == "__main__":
    main()
