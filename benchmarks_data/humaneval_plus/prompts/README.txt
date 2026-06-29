HumanEval+ не использует отдельный system prompt или LLM-judge.

Промпт для модели — это напрямую поле `prompt` из датасета (Python docstring + сигнатура функции).
Пример:
    def has_close_elements(numbers: List[float], closeness_threshold: float) -> bool:
        """ Check if in given list of numbers, are any two numbers closer to each other
        than given threshold.
        >>> has_close_elements([1.0, 2.0, 3.0], 0.5)
        False
        >>> has_close_elements([1.0, 2.8, 3.0, 4.0, 5.0, 2.0], 0.3)
        True
        """

Оценка: запуск сгенерированного кода против тест-кейсов (execution-based, не LLM-judge).
Функция оценки: evalplus/evaluate.py -> evaluate()
