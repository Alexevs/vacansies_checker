# imports

import os
from dotenv import load_dotenv
import requests
import json
import re
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import time
from openai import OpenAI
import telebot

# functions

HH_API_BASE_URL = 'https://api.hh.ru'
DEFAULT_HH_API_USER_AGENT = 'VacanciesBot/1.0 (support@vacancies-bot.dev)'
HH_API_TIMEOUT = 20


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'p', 'div', 'br', 'li', 'ul', 'ol'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'p', 'div', 'li', 'ul', 'ol'}:
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        raw_text = ''.join(self.parts)
        cleaned_text = re.sub(r'\n{3,}', '\n\n', raw_text)
        return '\n'.join(line.strip() for line in cleaned_text.splitlines() if line.strip())


def hh_get(endpoint, params=None):
    response = requests.get(
        f'{HH_API_BASE_URL}{endpoint}',
        params=params,
        headers={'User-Agent': os.getenv('HH_API_USER_AGENT', DEFAULT_HH_API_USER_AGENT)},
        timeout=HH_API_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def html_to_text(html_content):
    if not html_content:
        return ''

    parser = HTMLTextExtractor()
    parser.feed(unescape(html_content))
    parser.close()
    return parser.get_text()


def get_hh_vacancies(word):
    vacancies_list = {'items': []}

    params_dict = {
        'text': word,
        'per_page': 100,
        'schedule': 'fullDay',
        'work_format': 'REMOTE'
    }

    first_page = hh_get('/vacancies', params=params_dict)
    vacancies_list['items'] = first_page['items']
    pages = first_page['pages']

    if pages > 1:
        for i in range(1, pages):
            page_params = params_dict | {'page': i}
            page_data = hh_get('/vacancies', params=page_params)
            vacancies_list['items'].extend(page_data['items'])

    return vacancies_list

def read_existing_ids(filename):
    # Загружаем существующие данные
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        data = []

    # Собираем существующие id
    existing_ids = {item['id'] for item in data}
    return existing_ids

def update_vacancies_list(item, filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        data = []

    vacancy_details = hh_get(f"/vacancies/{item['id']}")
    employer = vacancy_details.get('employer', {})
    accredited_it_employer = employer.get('accredited_it_employer')
    if accredited_it_employer:
        IT = 'Да'
    else:
        IT = 'Нет'

    salary = vacancy_details.get('salary')
    if salary is not None:
        salary_from = salary.get('from')
        salary_to = salary.get('to')
        salary_cur = salary.get('currency')
    else:
        salary_from = None
        salary_to = None
        salary_cur = None

    vacancy_description = html_to_text(vacancy_details.get('description'))
    
    data.append({
        'id': vacancy_details['id'],
        'Название': vacancy_details['name'],
        'От': salary_from,
        'До': salary_to,
        'Валюта': salary_cur,
        'Описание': vacancy_description,
        'Кратко': False,
        'Компания': employer.get('name'),
        'IT': IT,
        'Опубликовано': vacancy_details['published_at'],
        'Ссылка': vacancy_details['alternate_url'],
        'Отправлено': False
    })

    # Сохраняем всё
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def llm_prompting(token, model, temperature, system, prompt):
    client = OpenAI(base_url = "https://openrouter.ai/api/v1", api_key = token)

    try:
        start_time = time.perf_counter()
        completion = client.chat.completions.create(
            extra_headers={},
            extra_body={},
            temperature=temperature,
            model = model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}]
        )
        answer = completion.choices[0].message.content
        llm_input = completion.usage.prompt_tokens
        llm_output = completion.usage.completion_tokens
        end_time = time.perf_counter()
        elapsed_time = int((end_time - start_time)*1000)

        return answer, llm_input, llm_output, elapsed_time
    except Exception as e:
        return f"Ошибка: {e}", 0, 0, 0 

def get_summary_from_llm(data_filename, prompt_filename, llm_settings):
    # Загружаем данные из файла
    with open(data_filename, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    for item in data:
        if not item['Кратко']:
            description = item['Описание']
            
            with open(prompt_filename, 'r', encoding='utf-8') as f:
                template = f.read()

            # Интерпорлируем описание вакансии в промт
            prompt = template.format(llm_input = description)

            # Считаем число слов в промте
            item['Вход_слов'] = prompt.count(' ')

            summary, in_tokens, out_tokens, elapsed_time = llm_prompting(llm_settings['api_key'], llm_settings['model'], 0.2, llm_settings['system_message'], prompt)
            
            item['Кратко'] = summary
            item['Вход_токенов'] = in_tokens
            item['Выход_токенов'] = out_tokens
            item['Время_генерации'] = elapsed_time
            item['Потрачено'] = llm_settings['input_cost']*in_tokens/1000 + llm_settings['output_cost']*out_tokens/1000

        # Сохраняем обратно в файл
        with open(data_filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def send_new_vacancies(data_filename, bot_token, channel_name):
    # Загружаем данные из файла
    with open(data_filename, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    bot = telebot.TeleBot(bot_token)

    for item in data:
        if not item['Отправлено']:       
            
            published = datetime.fromisoformat(item["Опубликовано"]).strftime("%d.%m.%Y")
            sal_f = '' if type(item["От"])!=int else f'От {item["От"]} '
            sal_t = '' if type(item["До"])!=int else f'до {item["До"]} '
            sal_c = '' if type(item["Валюта"])!=str else item["Валюта"]
                
            if sal_f==sal_t==sal_c:
                salary = 'Доход не указан'
            else:
                salary = sal_f + sal_t + sal_c

            rows = [f'*Вакансия* от {published}',
                    '',
                    f'[{item["Название"]}]({item["Ссылка"]})',
                    salary,
                    '',
                    item["Кратко"],
                    '',
                    f'_{item["Компания"]}, IT: {item["IT"]}_',
                    '',
                    f'Траты: {round(item["Потрачено"],2)} ₽ | {item["Время_генерации"]} мс | [Поддержать](https://tips.yandex.ru/guest/payment/3454449)'
                    ]

            message = '\n'.join(row for row in rows)

            bot.send_message(channel_name, message, parse_mode='Markdown', disable_web_page_preview=True)
            time.sleep(5)

            item['Отправлено'] = True

            with open(vacancies_filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

# variables

load_dotenv()
LLM_API_KEY = os.getenv('LLM_API_KEY')
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')

llm_settings = dict(api_key = LLM_API_KEY,
                    system_message = "Ты - большая языковая модель, личный помощник. Отвечай на вопросы пользователя на русском языке и точно по запросу.",
                    model = 'nvidia/nemotron-3-super-120b-a12b:free',
                    input_cost = 0.0,
                    output_cost = 0.0)

vacancies_filename ='vacancies.json'

# main

def main():
    current_vacancies = get_hh_vacancies('prompt')
    existing_ids = read_existing_ids(vacancies_filename)

    for item in current_vacancies['items']:
            if item['id'] not in existing_ids:
                update_vacancies_list(item, vacancies_filename)

    get_summary_from_llm(vacancies_filename, 'prompt_eng.prmpt', llm_settings)

    send_new_vacancies(vacancies_filename, TG_BOT_TOKEN, '@llmforall')

if __name__ == "__main__":
    main()
