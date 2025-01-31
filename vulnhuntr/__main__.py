import json
import re
import argparse
import structlog
from vulnhuntr.symbol_finder import SymbolExtractor
from vulnhuntr.LLMs import Claude, ChatGPT
from vulnhuntr.prompts import *
from rich import print
from typing import List, Generator
from enum import Enum
from pathlib import Path
from pydantic_xml import BaseXmlModel, element
from pydantic import BaseModel, Field


structlog.configure(
    processors=[
        structlog.processors.JSONRenderer()
    ],
    logger_factory=structlog.WriteLoggerFactory(
        file=Path('vulnhuntr').with_suffix(".log").open("wt")
    )
)

import faulthandler
faulthandler.enable()

log = structlog.get_logger("vulnhuntr")

class VulnType(str, Enum):
    LFI = "LFI"
    RCE = "RCE"
    SSRF = "SSRF"
    AFO = "AFO"
    SQLI = "SQLI"
    XSS = "XSS"
    IDOR = "IDOR"

class ContextCode(BaseModel):
    name: str = Field(description="Function or Class name")
    reason: str = Field(description="Brief reason why this function's code is needed for analysis")
    code_line: str = Field(description="The single line of code where where this context object is referenced.")

class Response(BaseModel):
    scratchpad: str = Field(description="Your step-by-step analysis process. Output in plaintext with no line breaks.")
    analysis: str = Field(description="Your final analysis. Output in plaintext with no line breaks.")
    poc: str = Field(description="Proof-of-concept exploit, if applicable.")
    confidence_score: int = Field(description="0-10, where 0 is no confidence and 10 is absolute certainty because you have the entire user input to server output code path.")
    vulnerability_types: List[VulnType] = Field(description="The types of identified vulnerabilities")
    context_code: List[ContextCode] = Field(description="List of context code items requested for analysis, one function or class name per item. No standard library or third-party package code.")

class ReadmeContent(BaseXmlModel, tag="readme_content"):
    content: str

class ReadmeSummary(BaseXmlModel, tag="readme_summary"):
    readme_summary: str

class Instructions(BaseXmlModel, tag="instructions"):
    instructions: str

class ResponseFormat(BaseXmlModel, tag="response_format"):
    response_format: str

class AnalysisApproach(BaseXmlModel, tag="analysis_approach"):
    analysis_approach: str

class Guidelines(BaseXmlModel, tag="guidelines"):
    guidelines: str

class FileCode(BaseXmlModel, tag="file_code"):
    file_path: str = element()
    file_source: str = element()

class PreviousAnalysis(BaseXmlModel, tag="previous_analysis"):
    previous_analysis: str

class ExampleBypasses(BaseXmlModel, tag="example_bypasses"):
    example_bypasses: str

class CodeDefinition(BaseXmlModel, tag="code"):
    name: str = element()
    context_name_requested: str = element()
    file_path: str = element()
    source: str = element()

class CodeDefinitions(BaseXmlModel, tag="context_code"):
    definitions: List[CodeDefinition] = []

class RepoOps:
    def __init__(self, repo_path: Path | str ) -> None:
        self.repo_path = Path(repo_path)
        self.to_exclude = {'/setup.py', '/test', '/example', '/docs', '/site-packages', '.venv', 'virtualenv', '/dist'}
        self.file_names_to_exclude = ['test_', 'conftest', '_test.py']
        # 对输入的待分析开源项目进行分析，根据文件名及文件中的语法判断使用哪些 web 应用框架
        patterns = [
            #Async
            r'async\sdef\s\w+\(.*?request',

            # Gradio
            r'gr.Interface\(.*?\)',
            r'gr.Interface\.launch\(.*?\)',

            # Flask
            r'@app\.route\(.*?\)',
            r'@blueprint\.route\(.*?\)',
            r'class\s+\w+\(MethodView\):',
            r'@(?:app|blueprint)\.add_url_rule\(.*?\)',

            # FastAPI
            r'@app\.(?:get|post|put|delete|patch|options|head|trace)\(.*?\)',
            r'@router\.(?:get|post|put|delete|patch|options|head|trace)\(.*?\)',

            # Django
            r'url\(.*?\)', #Too broad?
            r're_path\(.*?\)',
            r'@channel_layer\.group_add',
            r'@database_sync_to_async',

            # Pyramid
            r'@view_config\(.*?\)',

            # Bottle
            r'@(?:route|get|post|put|delete|patch)\(.*?\)',

            # Tornado
            r'class\s+\w+\((?:RequestHandler|WebSocketHandler)\):',
            r'@tornado\.gen\.coroutine',
            r'@tornado\.web\.asynchronous',

            #WebSockets
            r'websockets\.serve\(.*?\)',
            r'@websocket\.(?:route|get|post|put|delete|patch|head|options)\(.*?\)',

            # aiohttp
            r'app\.router\.add_(?:get|post|put|delete|patch|head|options)\(.*?\)',
            r'@routes\.(?:get|post|put|delete|patch|head|options)\(.*?\)',

            # Sanic
            r'@app\.(?:route|get|post|put|delete|patch|head|options)\(.*?\)',
            r'@blueprint\.(?:route|get|post|put|delete|patch|head|options)\(.*?\)',

            # Falcon
            r'app\.add_route\(.*?\)',

            # CherryPy
            r'@cherrypy\.expose',

            # web2py
            r'def\s+\w+\(\):\s*return\s+dict\(',

            # Quart (ASGI version of Flask)
            r'@app\.route\(.*?\)',
            r'@blueprint\.route\(.*?\)',

            # Starlette (which FastAPI is based on)
            r'@app\.route\(.*?\)',
            r'Route\(.*?\)',

            # Responder
            r'@api\.route\(.*?\)',

            # Hug
            r'@hug\.(?:get|post|put|delete|patch|options|head)\(.*?\)',

            # Dash (for analytical web applications)
            r'@app\.callback\(.*?\)',

            # GraphQL entry points
            r'class\s+\w+\(graphene\.ObjectType\):',
            r'@strawberry\.type',

            # Generic decorators that might indicate custom routing
            r'@route\(.*?\)',
            r'@endpoint\(.*?\)',
            r'@api\.\w+\(.*?\)',

            # AWS Lambda handlers (which could be used with API Gateway)
            r'def\s+lambda_handler\(event,\s*context\):',
            r'def\s+handler\(event,\s*context\):',

            # Azure Functions
            r'def\s+\w+\(req:\s*func\.HttpRequest\)\s*->',

            # Google Cloud Functions
            r'def\s+\w+\(request\):'

            # web 应用启动代码
            # Server startup code
            r'app\.run\(.*?\)',
            r'serve\(app,.*?\)',
            r'uvicorn\.run\(.*?\)',
            r'application\.listen\(.*?\)',
            r'run_server\(.*?\)',
            r'server\.start\(.*?\)',
            r'app\.listen\(.*?\)',
            r'httpd\.serve_forever\(.*?\)',
            r'tornado\.ioloop\.IOLoop\.current\(\)\.start\(\)',
            r'asyncio\.run\(.*?\.serve\(.*?\)\)',
            r'web\.run_app\(.*?\)',
            r'WSGIServer\(.*?\)\.serve_forever\(\)',
            r'make_server\(.*?\)\.serve_forever\(\)',
            r'cherrypy\.quickstart\(.*?\)',
            r'execute_from_command_line\(.*?\)',  # Django's manage.py
            r'gunicorn\.app\.wsgiapp\.run\(\)',
            r'waitress\.serve\(.*?\)',
            r'hypercorn\.run\(.*?\)',
            r'daphne\.run\(.*?\)',
            r'werkzeug\.serving\.run_simple\(.*?\)',
            r'gevent\.pywsgi\.WSGIServer\(.*?\)\.serve_forever\(\)',
            r'grpc\.server\(.*?\)\.start\(\)',
            r'app\.start_server\(.*?\)',  # Sanic
            r'Server\(.*?\)\.run\(\)',    # Bottle
        ]
        # 将正则表达式进行编译，输出正则表达式对象列表
        # Compile the patterns for efficiency
        self.compiled_patterns = [re.compile(pattern) for pattern in patterns]

    def get_readme_content(self) -> str:
        # Use glob to find README.md or README.rst in a case-insensitive manner in the root directory
        prioritized_patterns = ["[Rr][Ee][Aa][Dd][Mm][Ee].[Mm][Dd]", "[Rr][Ee][Aa][Dd][Mm][Ee].[Rr][Ss][Tt]"]
        
        # First, look for README.md or README.rst in the root directory with case insensitivity
        for pattern in prioritized_patterns:
            for readme in self.repo_path.glob(pattern):
                with readme.open(encoding='utf-8') as f:
                    return f.read()
                
        # If no README.md or README.rst is found, look for any README file with supported extensions
        for readme in self.repo_path.glob("[Rr][Ee][Aa][Dd][Mm][Ee]*.[Mm][DdRrSsTt]"):
            with readme.open(encoding='utf-8') as f:
                return f.read()
        
        return

    def get_relevant_py_files(self) -> Generator[Path, None, None]:
        """Gets all Python files in a repo minus the ones in the exclude list (test, example, doc, docs)"""
        files = []
        for f in self.repo_path.rglob("*.py"):
            # Convert the path to a string with forward slashes
            f_str = str(f).replace('\\', '/')
            
            # Lowercase the string for case-insensitive matching
            f_str = f_str.lower()

            # Check if any exclusion pattern matches a substring of the full path
            # 筛选掉非功能代码或文件
            if any(exclude in f_str for exclude in self.to_exclude):
                continue

            # Check if the file name should be excluded
            if any(fn in f.name for fn in self.file_names_to_exclude):
                continue
            
            files.append(f)

        return files

    def get_network_related_files(self, files: List) -> Generator[Path, None, None]:
        '''
        读取文件内容并进行正则表达式匹配，输出与用户输入相关的文件（web应用路由 api 函数、web 应用开启函数）
        '''
        for py_f in files:
            with py_f.open(encoding='utf-8') as f:
                content = f.read()
            if any(re.search(pattern, content) for pattern in self.compiled_patterns):
                yield py_f

    def get_files_to_analyze(self, analyze_path: Path | None = None) -> List[Path]:
        path_to_analyze = analyze_path or self.repo_path
        if path_to_analyze.is_file():
            return [ path_to_analyze ]
        elif path_to_analyze.is_dir():
            return path_to_analyze.rglob('*.py')
        else:
            raise FileNotFoundError(f"Specified analyze path does not exist: {path_to_analyze}")

def extract_between_tags(tag: str, string: str, strip: bool = False) -> list[str]:
    """
    https://github.com/anthropics/anthropic-cookbook/blob/main/misc/how_to_enable_json_mode.ipynb
    """
    ext_list = re.findall(f"<{tag}>(.+?)</{tag}>", string, re.DOTALL)
    if strip:
        ext_list = [e.strip() for e in ext_list]
    return ext_list

def print_readable(report: Response) -> None:
    for attr, value in vars(report).items():
        print(f"{attr}:")
        if isinstance(value, str):
            # For multiline strings, add indentation
            lines = value.split('\n')
            for line in lines:
                print(f"  {line}")
        elif isinstance(value, list):
            # For lists, print each item on a new line
            for item in value:
                print(f"  - {item}")
        else:
            # For other types, just print the value
            print(f"  {value}")
        print('-' * 40)
        print()  # Add an empty line between attributes

def run():
    '''
    可输入参数如下：
    --root: 待分析开源项目的根目录，必填
    --analyze: 指定需要分析的路径或文件
    --llm: 指定 llms，可选 claude、gpt
    --verbosity
    '''
    parser = argparse.ArgumentParser(description='Analyze a GitHub project for vulnerabilities. Export your ANTHROPIC_API_KEY before running.')
    parser.add_argument('-r', '--root', type=str, required=True, help='Path to the root directory of the project')
    parser.add_argument('-a', '--analyze', type=str, help='Specific path or file within the project to analyze')
    parser.add_argument('-l', '--llm', type=str, choices=['claude', 'gpt'], default='claude', help='LLM client to use (default: claude)')
    parser.add_argument('-v', '--verbosity', action='count', default=0, help='Increase output verbosity (-v for INFO, -vv for DEBUG)')
    args = parser.parse_args()

    repo = RepoOps(args.root)

    code_extractor = SymbolExtractor(args.root)
    # Get repo files that don't include stuff like tests and documentation
    # 得到一个不含非功能实现代码的文件列表
    files = repo.get_relevant_py_files()

    # User specified --analyze flag
    if args.analyze:
        # Determine the path to analyze
        analyze_path = Path(args.analyze)

        # If the path is absolute, use it as is, otherwise join it with the root path so user can specify relative paths
        if analyze_path.is_absolute():
            files_to_analyze = repo.get_files_to_analyze(analyze_path)
        else:
            files_to_analyze = repo.get_files_to_analyze(Path(args.root) / analyze_path)

    # Analyze the entire project for network-related files
    else:
        files_to_analyze = repo.get_network_related_files(files)

    if args.llm == 'claude':
        llm = Claude()
    elif args.llm == 'gpt':
        llm = ChatGPT()

    # 查找 readme 相关文件并进行读取分析
    readme_content = repo.get_readme_content()
    if readme_content:
        log.info("Summarizing project README")
        # chat(self, user_prompt: str, response_model: BaseModel = None, max_tokens: int = 4096) -> Union[BaseModel, str]
        # 使用 llm client 接口，分析 Readme 并返回回复并保持聊天上下文
        summary = llm.chat(
            (ReadmeContent(content=readme_content).to_xml() + b'\n' +
            Instructions(instructions=README_SUMMARY_PROMPT_TEMPLATE).to_xml()
            ).decode()
        )
        summary = extract_between_tags("summary", summary)[0]
        log.info("README summary complete", summary=summary)
    else:
        log.warning("No README summary found")
        summary = ''

    # files_to_analyze is either a list of all network-related files or a list containing a single file/dir to analyze
    for py_f in files_to_analyze:
        log.info(f"Performing initial analysis", file=str(py_f))

        system_prompt = (Instructions(instructions=SYS_PROMPT_TEMPLATE).to_xml() + b'\n' +
                        ReadmeSummary(readme_summary=summary).to_xml()
                        ).decode()
        # 待确认其输出
        print("\n------------ DEBUG system_prompt ----------------\n")
        print(system_prompt)
        # This is the Initial analysis
        with py_f.open(encoding='utf-8') as f:
            content = f.read()
            if not len(content):
                continue

            print(f"\nAnalyzing {py_f}")
            print('-' * 40 +'\n')

            if args.llm == 'claude':
                llm = Claude(system_prompt=system_prompt)
            elif args.llm == 'gpt':
                llm = ChatGPT(system_prompt=system_prompt)

            user_prompt =(
                    FileCode(file_path=str(py_f), file_source=content).to_xml() + b'\n' +
                    Instructions(instructions=INITIAL_ANALYSIS_PROMPT_TEMPLATE).to_xml() + b'\n' +
                    AnalysisApproach(analysis_approach=ANALYSIS_APPROACH_TEMPLATE).to_xml() + b'\n' +
                    PreviousAnalysis(previous_analysis='').to_xml() + b'\n' +
                    Guidelines(guidelines=GUIDELINES_TEMPLATE).to_xml() + b'\n' +
                    ResponseFormat(response_format=json.dumps(Response.model_json_schema(), indent=4
                    )
                ).to_xml()
            ).decode()
            print("\n------------ DEBUG user_prompt ----------------\n")
            print(user_prompt)
            initial_analysis_report: Response = llm.chat(user_prompt, response_model=Response)
            log.info("Initial analysis complete", report=initial_analysis_report.model_dump())
            # 初步分析的响应文本
            print("\n---------- 输出初次分析的结果 ------------------\n")
            print_readable(initial_analysis_report)

            # Secondary analysis
            if initial_analysis_report.confidence_score > 0 and len(initial_analysis_report.vulnerability_types):

                for vuln_type in initial_analysis_report.vulnerability_types:

                    # Do not fetch the context code on the first pass of the secondary analysis because the context will be from the general analysis
                    stored_code_definitions = {}
                    definitions = CodeDefinitions(definitions=[])
                    same_context = False

                    # Don't include the intial analysis or the first iteration of the secondary analysis in the user_prompt
                    previous_analysis = ''
                    previous_context_amount = 0

                    for i in range(7):
                        log.info(f"Performing vuln-specific analysis", iteration=i, vuln_type=vuln_type, file=py_f)

                        # Only lookup context code and previous analysis on second pass and onwards
                        if i > 0:
                            previous_context_amount = len(stored_code_definitions)
                            previous_analysis = secondary_analysis_report.analysis

                            for context_item in secondary_analysis_report.context_code:
                                # Make sure bot isn't requesting the same code multiple times
                                if context_item.name not in stored_code_definitions:
                                    name = context_item.name
                                    code_line = context_item.code_line
                                    match = code_extractor.extract(name, code_line, files)
                                    if match:
                                        stored_code_definitions[name] = match

                            code_definitions = list(stored_code_definitions.values())
                            definitions = CodeDefinitions(definitions=code_definitions)
                            
                            if args.verbosity > 1:
                                for definition in definitions.definitions:
                                    if '\n' in definition.source:
                                        lines = definition.source.split('\n')
                                        snippet = lines[0] + '\n' + lines[1]
                                    else:
                                        snippet = definition.source[:75]
                                    
                                    print(f"Name: {definition.name}")
                                    print(f"Context search: {definition.context_name_requested}")
                                    print(f"File Path: {definition.file_path}")
                                    print(f"First two lines from source: {snippet}\n")

                        vuln_specific_user_prompt = (
                            FileCode(file_path=str(py_f), file_source=content).to_xml() + b'\n' +
                            definitions.to_xml() + b'\n' +  # These are all the requested context functions and classes
                            ExampleBypasses(
                                example_bypasses='\n'.join(VULN_SPECIFIC_BYPASSES_AND_PROMPTS[vuln_type]['bypasses'])
                            ).to_xml() + b'\n' +
                            Instructions(instructions=VULN_SPECIFIC_BYPASSES_AND_PROMPTS[vuln_type]['prompt']).to_xml() + b'\n' +
                            AnalysisApproach(analysis_approach=ANALYSIS_APPROACH_TEMPLATE).to_xml() + b'\n' +
                            PreviousAnalysis(previous_analysis=previous_analysis).to_xml() + b'\n' +
                            Guidelines(guidelines=GUIDELINES_TEMPLATE).to_xml() + b'\n' +
                            ResponseFormat(
                                response_format=json.dumps(
                                    Response.model_json_schema(), indent=4
                                )
                            ).to_xml()
                        ).decode()
                        print("\n------------------- vuln_specific_user_prompt -----------------\n")
                        print(vuln_specific_user_prompt)
                        secondary_analysis_report: Response = llm.chat(vuln_specific_user_prompt, response_model=Response)
                        log.info("Secondary analysis complete", secondary_analysis_report=secondary_analysis_report.model_dump())

                        if args.verbosity > 0:
                            print_readable(secondary_analysis_report)

                        if not len(secondary_analysis_report.context_code):
                            log.debug("No new context functions or classes found")
                            if args.verbosity == 0:
                                print_readable(secondary_analysis_report)
                            break
                        
                        # Check if any new context code is requested
                        if previous_context_amount >= len(stored_code_definitions) and i > 0:
                            # Let it request the same context once, then on the second time it requests the same context, break
                            if same_context:
                                log.debug("No new context functions or classes requested")
                                if args.verbosity == 0:
                                    print_readable(secondary_analysis_report)
                                break
                            same_context = True
                            log.debug("No new context functions or classes requested")
                    pass

if __name__ == '__main__':
    run()
