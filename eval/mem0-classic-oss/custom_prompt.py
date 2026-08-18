# HaluMem 원본이 mem0 Platform에 주입한 custom instructions
# (HaluMem/eval/eval_memzero.py:27-53에서 복사 — 수정 금지)
HALUMEM_CUSTOM_INSTRUCTIONS = """
Generate personal memories that follow these guidelines:

1. Each memory should be self-contained with complete context, including:
   - The person's name, do not use "user" while creating memories
   - Personal details (career aspirations, hobbies, life circumstances)
   - Emotional states and reactions
   - Ongoing journeys or future plans
   - Specific dates when events occurred

2. Include meaningful personal narratives focusing on:
   - Identity and self-acceptance journeys
   - Family planning and parenting
   - Creative outlets and hobbies
   - Mental health and self-care activities
   - Career aspirations and education goals
   - Important life events and milestones

3. Make each memory rich with specific details rather than general statements
   - Include timeframes (exact dates when possible)
   - Name specific activities (e.g., "charity race for mental health" rather than just "exercise")
   - Include emotional context and personal growth elements

4. Extract memories only from user messages, not incorporating assistant responses

5. Format each memory as a paragraph with a clear narrative structure that captures the person's experience, challenges, and aspirations
"""

# OSS의 custom_fact_extraction_prompt는 기본 프롬프트를 통째로 대체함
# -> 파서(json.loads(response)["facts"])가 요구하는 출력 포맷 지시만 최소한으로 덧붙임
CUSTOM_FACT_EXTRACTION_PROMPT = HALUMEM_CUSTOM_INSTRUCTIONS + """
Return the memories in JSON format with a "facts" key, e.g. {"facts": ["memory 1", "memory 2"]}.
If there is nothing worth remembering, return {"facts": []}.
"""