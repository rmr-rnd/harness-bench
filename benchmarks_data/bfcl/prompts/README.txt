BFCL не использует отдельный system prompt или LLM-judge.

Промпт для модели формируется из полей задачи:
- question: список сообщений [{"role": "user", "content": "..."}]
- function: список JSON-определений доступных функций (передаётся как tools/functions)

Пример:
    question: [{"role": "user", "content": "Calculate the circumference of a circle with radius 3"}]
    function: [{"name": "geometry.circumference", "description": "...", "parameters": {...}}]

Оценка: AST-сравнение tool call ответа модели с ground truth из possible_answer/.
Функция оценки: bfcl_eval/eval_checker/ast_eval/ast_checker.py
Не LLM-judge — детерминированная проверка структуры вызова функции.
