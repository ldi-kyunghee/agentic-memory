import yaml

PROMPT = (
    "Answer the following question based on the documents provided."
    "Documents: "
    "{documents}"
    "Question: {question}"
    "If the answer could not be found in the provided documents, do not hallucinate an answer."
    "Answer concisely without commentary."
).strip()

def load_config(config_file):
    with open(f"configs/{config_file}", "r") as file:
        model_kwargs = yaml.safe_load(file)
    
    sampling_params = model_kwargs.pop("sampling_params")
    return model_kwargs, sampling_params
