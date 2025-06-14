import json
import re
import sys
import os

import numpy as np
import pandas as pd
import torch
from dotenv import load_dotenv
from google import genai
from sklearn.model_selection import train_test_split
from tqdm import tqdm
from typing import List
from sentence_transformers import SentenceTransformer
from transformers import (AutoModelForCausalLM, AutoModelForSeq2SeqLM,
                          AutoTokenizer)

from ares.LLM_as_a_Judge_Adaptation.Filter_Synthetic_Queries import (filter_synthetic_queries,
                                                                      generate_additional_negatives,
                                                                      generate_additional_positives,
                                                                      generate_index)
from ares.LLM_as_a_Judge_Adaptation.LLM_Generation_Functions import (check_generated_answer,
                                                                      generate_answer_llm_approach,
                                                                      generate_synthetic_query_llm_approach,
                                                                      )

from ares.LLM_as_a_Judge_Adaptation.LLM_Synthetic_Generation import (generate_synthetic_query_api_approach,
                                                                    generate_synthetic_query_gemini_approach,
                                                                    generate_synthetic_answer_gemini_approach)


import os


pd.set_option('display.max_columns', None) 
pd.set_option('display.max_rows', None)  
pd.set_option('display.max_colwidth', None) 

def clean_document(document: str) -> str:
    """
    Cleans the input document by removing unnecessary whitespace characters and replacing certain punctuation.

    Args:
        document (str): The original document text that needs to be cleaned.

    Returns:
        str: The cleaned document text.
    """
    # Replace carriage returns and tabs with a space, and reduce multiple newlines to a single newline
    cleaned_document = re.sub(r'\n+', '\n', document.replace("\r", " ").replace("\t", " ")).strip()
    # Replace equals signs and hyphens with spaces
    cleaned_document = cleaned_document.replace("=", " ").replace("-", " ")
    # Reduce multiple spaces to a single space
    cleaned_document = re.sub(r'\s+', ' ', cleaned_document).strip()
    # Join words with a single space (this line seems redundant and could be removed if confirmed)
    cleaned_document = (" ").join(cleaned_document.split(" "))  # [:512] - this part is commented out and can be ignored or removed
    return cleaned_document

def validate_input_file(df: pd.DataFrame, required_columns: list[str]) -> bool:
    """
    Validates that the DataFrame contains all required columns. Exits the program if any are missing.

    Args:
        df (pd.DataFrame): The DataFrame to validate.
        required_columns (List[str]): A list of strings representing the column names that are required in the DataFrame.

    Returns:
        bool: True if the DataFrame contains all required columns, otherwise the program will exit with an error.
    """
    # Identify any missing columns
    missing_columns = [col for col in required_columns if col not in df.columns]
    # Exit the program with an error message if there are missing columns
    if missing_columns:
        sys.exit(f"Error: The DataFrame is missing the following required column(s): {', '.join(missing_columns)}.")
    return True

def load_model(model_choice: str, api_model: bool, vllm: bool) -> tuple:
    """
    Loads the specified model and tokenizer, and sets the model to evaluation mode on the appropriate device.

    Args:
        model_choice (str): The model identifier to load from the Hugging Face model hub.

    Returns:
        tuple: A tuple containing the model, tokenizer, and device.
    """

    if api_model: 
        return model_choice, None, None

    if vllm:
        tokenizer = AutoTokenizer.from_pretrained(model_choice)
        return model_choice, tokenizer, None

    if "Llama" in model_choice:
        tokenizer = AutoTokenizer.from_pretrained(model_choice)
        model = AutoModelForCausalLM.from_pretrained(model_choice)
    else:   
        tokenizer = AutoTokenizer.from_pretrained(model_choice)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_choice)

    # Disable gradient calculations and set the model to evaluation mode
    torch.no_grad()
    model.eval()

    # Set the device to CUDA if available
    device = torch.device("cuda:0")
    model.to(device)
    
    return model, tokenizer, device

def load_documents(document_filepath: str, clean_documents: bool, documents_sampled: int) -> pd.DataFrame:
    """
    Loads and processes documents for synthetic query and answer generation.

    Args:
        document_filepath (str): The path to the document file.
        clean_documents (bool): Flag indicating whether to clean the documents.
        documents_sampled (int): The number of documents to sample.

    Returns:
        pd.DataFrame: A DataFrame containing the processed documents.
    """
    documents = []
    # required_columns = ['Query', 'Document', 'Answer']
    required_columns = ['Document']

    if "docs_aws" in document_filepath:
        with open(document_filepath, "r") as json_file:
            json_data = json.load(json_file)
            documents = [x['text'] for x in json_data]

            # Clean document
            if clean_documents:
                documents = [clean_document(text) for text in documents]

        documents = pd.DataFrame(documents, columns=["document"])
    else:
        if not document_filepath.endswith('.tsv'):
            sys.exit(f"Error: The file {document_filepath} is not a TSV file.")
        try:
            documents = pd.read_csv(document_filepath, sep="\t")
            validate_input_file(documents, required_columns)
            documents.rename(columns={"Document": "document"}, inplace=True)
            documents['document'] = documents['document'].str.strip()
        except Exception as e:
            sys.exit(f"Error reading the file {document_filepath}: {e}")

    initial_count = len(documents)
    documents = documents[documents['document'].str.split().apply(len) >= 50]  # Filter documents with less than 50 words.
    after_filter_count = len(documents)

    count = initial_count - after_filter_count

    if after_filter_count == 0:
        sys.exit("All documents were less than 50 words, please provide a dataset with documents containing more than 50 words.")

    if documents_sampled > initial_count:
        print(f"\nThe `documents_sampled` parameter ({documents_sampled}) exceeds the available number of documents ({initial_count}). Sampling will be adjusted to the maximum available documents ({initial_count}).\n")
        documents_sampled = initial_count

    if count > 0:
        print(f"Filtered out {count} documents because they had less than 50 words.")
        if documents_sampled > after_filter_count: 
            print(f"Document sample is greater than document count. Sampling will be adjusted to {after_filter_count} documents\n")
            documents_sampled = after_filter_count

    documents = documents.sample(n=documents_sampled)

    return documents

def load_documents_from_json_folder(folder_path: str, clean_documents: bool, documents_sampled: int) -> pd.DataFrame:
    """
    Loads and processes structured JSON documents from a directory.

    Args:
        folder_path (str): The path to the directory containing JSON files.
        clean_documents (bool): Whether to clean the document texts.
        documents_sampled (int): Number of documents to sample.

    Returns:
        pd.DataFrame: A DataFrame with processed documents.
    """
    documents = []
    file_list = [f for f in os.listdir(folder_path) if f.endswith(".json")]

    for file in file_list:
        file_path = os.path.join(folder_path, file)
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Combine text from all fields except excluded ones
        excluded_keys = {"Extracted Figure", "Further Reading"}
        combined_text = ""
        for key, value in data.items():
            if key not in excluded_keys:
                combined_text += f"{key}:\n{value}\n\n"

        # Add figure descriptions if available
        descriptions = []
        for figure_group in data.get("Extracted Figure", []):
            for figure in figure_group:
                desc = figure.get("Description", "")
                if desc:
                    descriptions.append(desc)

        if descriptions:
            combined_text += "Figure Descriptions:\n"
            combined_text += "\n".join(descriptions)

        # Optionally clean the document
        if clean_documents:
            combined_text = clean_document(combined_text)  # Make sure you define this function

        documents.append(combined_text.strip())

    # Create DataFrame
    df = pd.DataFrame(documents, columns=["document"])

    # Filter out documents with fewer than 50 words
    initial_count = len(df)
    df = df[df['document'].str.split().apply(len) >= 50]
    after_filter_count = len(df)

    if after_filter_count == 0:
        sys.exit("All documents were less than 50 words. Please provide longer documents.")

    if documents_sampled > initial_count:
        print(f"\n`documents_sampled` ({documents_sampled}) > available ({initial_count}). Adjusting to {initial_count}.\n")
        documents_sampled = initial_count

    if initial_count - after_filter_count > 0:
        print(f"Filtered out {initial_count - after_filter_count} documents with fewer than 50 words.")
        if documents_sampled > after_filter_count:
            print(f"Sampling adjusted to {after_filter_count} after filtering.")
            documents_sampled = after_filter_count

    # Sample
    df = df.sample(n=documents_sampled)

    return df


def load_pdfs(files_path: str) -> List[bytes]:
    """
    Load all PDF files from a directory and return a list of their byte content.
    
    Args:
        files_path (str): Path to the directory containing PDF files.
    
    Returns:
        List[bytes]: A list where each item is the byte content of a PDF.
    """
    docs_data = []

    for file in os.listdir(files_path):
        if file.lower().endswith(".pdf"):  # ensure it's a PDF
            pdf_path = os.path.join(files_path, file)
            with open(pdf_path, "rb") as f:
                doc_data = f.read()
            docs_data.append(doc_data)
        
    return docs_data


def load_few_shot_prompt(few_shot_prompt_filename: str, for_fever_dataset: bool, for_wow_dataset: bool) -> tuple[str, int]:
    """
    Loads and processes a few-shot prompt from a TSV file.

    Args:
        few_shot_prompt_filename (str): The filename of the TSV file containing the few-shot prompts.
        for_fever_dataset (bool): Flag indicating if the prompts are for the FEVER dataset.
        for_wow_dataset (bool): Flag indicating if the prompts are for the WoW dataset.

    Returns:
        tuple[str, int]: A tuple containing the few-shot examples as a string and the length of the few-shot prompt.
    """
    few_shot_prompt = pd.read_csv(few_shot_prompt_filename, sep="\t")
    few_shot_prompt = few_shot_prompt[few_shot_prompt['Context_Relevance_Label'] == "[[Yes]]"]
    
    if "Query" not in few_shot_prompt:
        few_shot_prompt['Query'] = few_shot_prompt['Question']

    length_of_fewshot_prompt = len(few_shot_prompt)
    few_shot_examples = ""

    for row in range(len(few_shot_prompt)):
        few_shot_examples += f"Example {row + 1}:\n"
        few_shot_examples += f"Document: {clean_document(few_shot_prompt.iloc[row]['Document'])}\n"
        
        if for_fever_dataset:
            few_shot_examples += f"Statement: {few_shot_prompt.iloc[row]['Query']}\n\n"
        elif for_wow_dataset:
            few_shot_examples += f"Dialogue: {few_shot_prompt.iloc[row]['Query']}\n\n"
        else:
            few_shot_examples += f"Question: {few_shot_prompt.iloc[row]['Query']}\n\n"

    return few_shot_examples, length_of_fewshot_prompt

def load_few_shot_prompt_from_md(few_shot_prompt_filename: str) -> str:
    with open(few_shot_prompt_filename, 'r', encoding='utf-8') as file:
        prompt = file.read()

    prompt += "\nYour generated query\n<Query>"
    return prompt
    
# def generate_contradictory_answers(few_shot_prompt_filename: str, for_fever_dataset: bool, for_wow_dataset: bool) -> str:
#     """
#     Generates few-shot examples for contradictory answers based on the provided dataset.

#     Args:
#         few_shot_prompt_filename (str): The filename of the TSV file containing the few-shot prompts.
#         for_fever_dataset (bool): Flag indicating if the prompts are for the FEVER dataset.
#         for_wow_dataset (bool): Flag indicating if the prompts are for the WoW dataset.

#     Returns:
#         str: A string containing the few-shot examples for contradictory answers.
#     """
#     # Load the few-shot prompt data
#     few_shot_prompt_for_contradictory_answers = pd.read_csv(few_shot_prompt_filename, sep="\t")
#     few_shot_prompt_for_contradictory_answers = few_shot_prompt_for_contradictory_answers[
#         few_shot_prompt_for_contradictory_answers['Contradictory_Answer'].str.len() > 4
#     ]

#     # Initialize the few-shot examples string
#     few_shot_examples_for_contradictory_answers = ""

#     for row in range(len(few_shot_prompt_for_contradictory_answers)):
#         few_shot_examples_for_contradictory_answers += f"Example {row + 1}:\n"
#         few_shot_examples_for_contradictory_answers += f"Document: {few_shot_prompt_for_contradictory_answers.iloc[row]['Document']}\n"
        
#         if for_fever_dataset:
#             few_shot_examples_for_contradictory_answers += f"Statement: {few_shot_prompt_for_contradictory_answers.iloc[row]['Query']}\n"
#             few_shot_examples_for_contradictory_answers += f"Incorrect Answer: {few_shot_prompt_for_contradictory_answers.iloc[row]['Contradictory_Answer']}\n\n"
#         elif for_wow_dataset:
#             few_shot_examples_for_contradictory_answers += f"Dialogue: {few_shot_prompt_for_contradictory_answers.iloc[row]['Query']}\n"
#             few_shot_examples_for_contradictory_answers += f"Incorrect Response: {few_shot_prompt_for_contradictory_answers.iloc[row]['Contradictory_Answer']}\n\n"
#         else:
#             few_shot_examples_for_contradictory_answers += f"Question: {few_shot_prompt_for_contradictory_answers.iloc[row]['Query']}\n"
#             few_shot_examples_for_contradictory_answers += f"Incorrect Answer: {few_shot_prompt_for_contradictory_answers.iloc[row]['Contradictory_Answer']}\n\n"

#     return few_shot_examples_for_contradictory_answers

def generate_few_shot_prompts(few_shot_prompt_filename: str, for_fever_dataset: bool, for_wow_dataset: bool) -> tuple[str, int]:
    """
    Generates few-shot prompts for answer generation based on the provided dataset.

    Args:
        few_shot_prompt_filename (str): The filename of the TSV file containing the few-shot prompts.
        for_fever_dataset (bool): Flag indicating if the prompts are for the FEVER dataset.
        for_wow_dataset (bool): Flag indicating if the prompts are for the WoW dataset.

    Returns:
        tuple: A tuple containing the few-shot examples string and the length of the few-shot prompt.
    """
    # Load the few-shot prompt data
    answer_gen_few_shot_prompt = pd.read_csv(few_shot_prompt_filename, sep="\t")
    
    # Filter the prompts based on relevance and faithfulness labels
    answer_gen_few_shot_prompt = answer_gen_few_shot_prompt[
        (answer_gen_few_shot_prompt['Answer_Relevance_Label'] == "[[Yes]]") & 
        (answer_gen_few_shot_prompt['Answer_Faithfulness_Label'] == "[[Yes]]")
    ]
    
    # Get the length of the few-shot prompt
    length_of_fewshot_prompt_answer_gen = len(answer_gen_few_shot_prompt)
    
    # Rename 'Query' column to 'Question' if it exists
    if "Query" in answer_gen_few_shot_prompt.columns:
        answer_gen_few_shot_prompt['Question'] = answer_gen_few_shot_prompt['Query']
    
    # Initialize the few-shot examples string
    answer_gen_few_shot_examples = ""
    
    # Construct the few-shot examples
    for row in range(len(answer_gen_few_shot_prompt)):
        answer_gen_few_shot_examples += f"Example {row + 1}:\n"
        answer_gen_few_shot_examples += f"Document: {answer_gen_few_shot_prompt.iloc[row]['Document']}\n"
        
        if for_fever_dataset:
            answer_gen_few_shot_examples += f"Statement: {answer_gen_few_shot_prompt.iloc[row]['Query']}\n"
            answer_gen_few_shot_examples += f"Answer: {answer_gen_few_shot_prompt.iloc[row]['Answer']}\n\n"
        elif for_wow_dataset:
            answer_gen_few_shot_examples += f"Dialogue: {answer_gen_few_shot_prompt.iloc[row]['Query']}\n"
            answer_gen_few_shot_examples += f"Response: {answer_gen_few_shot_prompt.iloc[row]['Answer']}\n\n"
        else:
            answer_gen_few_shot_examples += f"Question: {answer_gen_few_shot_prompt.iloc[row]['Query']}\n"
            answer_gen_few_shot_examples += f"Answer: {answer_gen_few_shot_prompt.iloc[row]['Answer']}\n\n"
    
    return answer_gen_few_shot_examples, length_of_fewshot_prompt_answer_gen

def generate_query(document: bytes, settings: dict, client) -> list:
    """
    Generates synthetic queries for a given document.

    Args:
        document (str): The document text.
        settings (dict): Dictionary containing various settings and parameters required for generating synthetic queries.

    Returns:
        list: List of generated synthetic queries.
    """

    return generate_synthetic_query_gemini_approach( # LLM_Synth_Gen
        document, 
        settings["synthetic_query_prompt"], 
        settings['few_shot_examples'], 
        settings['model_name'], 
        settings['percentiles'],
        client=client
    )
    # else: 
    #     return generate_synthetic_query_llm_approach( # LLM_Generation
    #     document, 
    #     settings['few_shot_examples'], 
    #     settings['length_of_fewshot_prompt'], 
    #     settings['device'], 
    #     settings['tokenizer'], 
    #     settings['model'], 
    #     settings['percentiles'], 
    #     settings['for_fever_dataset'], 
    #     settings['for_wow_dataset']
    #     )

# import logging

# Configure logging
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

#ga1nang change
# def generate_positive_synthetic_queries(documents: pd.DataFrame, settings: dict, chunk_size: int) -> pd.DataFrame:
#     """
#     Processes the documents to generate synthetic queries and remove duplicates.

#     Args:
#         documents (pd.DataFrame): DataFrame containing the documents.
#         settings (dict): Dictionary containing various settings and parameters required for generating synthetic queries.
#         chunk_size (int): Number of documents to process in each chunk.

#     Returns:
#         pd.DataFrame: DataFrame containing the documents with the generated synthetic queries.
#     """
#     num_documents = len(documents)
#     initial_queries_per_document = 2
#     target_queries = num_documents * initial_queries_per_document # Targeting at least 2 queries per document

#     all_queries = [] # Stores all generated queries
#     synthetic_queries_filename = settings.get('synthetic_queries_filename', 'intermediate_queries.tsv')

#     # Process documents in chunks (useful if working with large datasets)
#     for start in range(0, num_documents, chunk_size):
#         end = min(start + chunk_size, num_documents)
#         chunk = documents[start:end]

#         # Show progress bar while generating queries for the chunk
#         with tqdm(total=len(chunk) * initial_queries_per_document, desc=f"Generating positive synthetic queries for documents {start} to {end}...") as pbar:
#             for index, row in chunk.iterrows():
#                 document = row['document']
#                 synthetic_queries = []
#                 for _ in range(initial_queries_per_document):
#                     # Call the query generator function
#                     synthetic_queries.extend(generate_query(document, settings))
#                     pbar.update(1)
#                 # Store the index, document, and all generated queries
#                 all_queries.append((index, document, synthetic_queries))

#         # Flatten the nested list of queries to a row per query
#         all_queries_flat = [(index, document, query) for index, document, queries in all_queries for query in queries]
#         synthetic_queries_df = pd.DataFrame(all_queries_flat, columns=["document_index", "document", "synthetic_query"])

#         print(f"Total queries generated before filtering: {len(synthetic_queries_df)}")

#         # Filter out short queries (likely noise or errors)
#         synthetic_queries_df = synthetic_queries_df[synthetic_queries_df["synthetic_query"].str.len() > 10]
#         print(f"Total queries after length filtering: {len(synthetic_queries_df)}")

#         # Remove exact duplicate queries
#         synthetic_queries_df = synthetic_queries_df.drop_duplicates(subset=['synthetic_query'])
#         print(f"Total queries after deduplication: {len(synthetic_queries_df)}")
        
#         # Build an index for filtering
#         document_index = generate_index(documents)
#         # Optional filter step to ensure quality or match-specific criteria
#         synthetic_queries_df = filter_synthetic_queries(synthetic_queries_df, document_index)
#         print(f"Total queries after filtering: {len(synthetic_queries_df)}")

#         # If we still don't have enough queries, generate more for under-represented documents
#         while len(synthetic_queries_df) < target_queries:
#             print(f"Not enough queries. Generating more...")
#             # Find which documents have < 1 valid query
#             counts = synthetic_queries_df['document_index'].value_counts()
#             documents_needing_more_queries = counts[counts < 1].index.tolist()

#             additional_queries = []
#             with tqdm(total=len(documents_needing_more_queries) * initial_queries_per_document, desc="Generating additional synthetic queries...") as pbar:
#                 for index in documents_needing_more_queries:
#                     document = documents.loc[index, 'document']
#                     for _ in range(initial_queries_per_document):
#                         additional_queries.extend(generate_query(document, settings))
#                         pbar.update(1)
#                     all_queries.append((index, document, additional_queries))

#             # Flatten additional queries
#             additional_queries_flat = [(index, document, query) for index, document, queries in additional_queries for query in queries]
#             additional_queries_df = pd.DataFrame(additional_queries_flat, columns=["document_index", "document", "synthetic_query"])
#             print(f"Additional queries generated before filtering: {len(additional_queries_df)}")

#             # Filter out too-short queries
#             additional_queries_df = additional_queries_df[additional_queries_df["synthetic_query"].str.len() > 10]
#             print(f"Additional queries after length filtering: {len(additional_queries_df)}")

#             # Combine with previous queries and re-apply filters
#             synthetic_queries_df = pd.concat([synthetic_queries_df, additional_queries_df]).drop_duplicates(subset=['synthetic_query'])
#             synthetic_queries_df = filter_synthetic_queries(synthetic_queries_df, document_index)
#             print(f"Total queries after adding additional queries and filtering: {len(synthetic_queries_df)}")

#         # Save intermediate results
#         synthetic_queries_df.to_csv(synthetic_queries_filename, mode='a', header=not os.path.exists(synthetic_queries_filename), index=False, sep="\t")

#     return synthetic_queries_df

def generate_positive_synthetic_queries(documents: list, settings: dict) -> pd.DataFrame:
    """
    Generate synthetic queries from a list of PDF documents (bytes), using Gemini.
    
    Args:
        documents (list): List of PDF files as byte streams.
        settings (dict): Configuration settings including model info and filename.
    
    Returns:
        pd.DataFrame: DataFrame of generated synthetic queries.
    """
    initial_queries_per_document = 2
    target_queries = len(documents) * initial_queries_per_document
    synthetic_queries_filename = settings.get('synthetic_queries_filename', 'intermediate_queries.tsv')

    all_queries = []
    
    #load model
    load_dotenv()
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    with tqdm(total=len(documents) * initial_queries_per_document, desc="Generating positive synthetic queries") as pbar:
        for doc_index, doc_bytes in enumerate(documents):
            for _ in range(initial_queries_per_document):
                try:
                    queries = generate_query(doc_bytes, settings, client)  # Must return list of strings
                    for query in queries:
                        all_queries.append({
                            "document_index": doc_index,
                            "document": doc_bytes,
                            "synthetic_query": query
                        })
                except Exception as e:
                    print(f"[Error] Doc {doc_index}: {e}")
                pbar.update(1)

    df = pd.DataFrame(all_queries)
    print(f"Generated: {len(df)} queries before filtering")

    df = df[df["synthetic_query"].str.len() > 10]
    df = df.drop_duplicates(subset=["synthetic_query"])
    print(f"After length + duplicate filtering: {len(df)}")

    # Apply filtering based on embedding/index
    embedding_model = SentenceTransformer('pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb')
    document_index = generate_index(documents, embedding_model=embedding_model)  # Should support list of bytes
    df = filter_synthetic_queries(df, document_index, embedding_model=embedding_model)
    print(f"After filter_synthetic_queries(): {len(df)}")

    # Check if we have <1 query per document and fix
    counts = df['document_index'].value_counts().to_dict()
    docs_missing = [i for i in range(len(documents)) if counts.get(i, 0) < 1]

    if docs_missing:
        print("Generating additional queries for underrepresented documents...")
        for index in tqdm(docs_missing, desc="Additional generation"):
            try:
                more_queries = generate_query(documents[index], settings, client)
                for query in more_queries:
                    all_queries.append({
                        "document_index": index,
                        "document": documents[index],
                        "synthetic_query": query
                    })
            except Exception as e:
                print(f"[Additional Error] Doc {index}: {e}")

        df = pd.DataFrame(all_queries)
        df = df[df["synthetic_query"].str.len() > 10]
        df = df.drop_duplicates(subset=["synthetic_query"])
        df = filter_synthetic_queries(df, document_index, embedding_model=embedding_model)
        print(f"Final query count after retries: {len(df)}")

    # Save results
    df.to_csv(
        synthetic_queries_filename,
        mode='a',
        header=not os.path.exists(synthetic_queries_filename),
        index=False,
        sep="\t"
    )

    return df

def generate_negative_synthetic_queries(positive_queries_df: pd.DataFrame, documents: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """
    Generates negative synthetic queries by randomly sampling the positive queries for the remaining documents.

    Args:
        positive_queries_df (pd.DataFrame): DataFrame containing the positive synthetic queries.
        documents (pd.DataFrame): DataFrame containing the documents.
        settings (dict): Dictionary containing various settings and parameters required for generating synthetic queries.

    Returns:
        pd.DataFrame: DataFrame containing both positive and negative synthetic queries.
    """
    documents_df = pd.DataFrame(documents, columns=["document"])
    negative_queries = []
    used_queries = set()
    sampled_queries = positive_queries_df['synthetic_query'].values

    for index, row in documents_df.iterrows():
        document = row['document']
        negative_query = None

        while negative_query is None or negative_query in used_queries:
            negative_query = np.random.choice(sampled_queries)

        used_queries.add(negative_query)
        negative_queries.append((index, document, negative_query))

    negative_queries_df = pd.DataFrame(negative_queries, columns=["document_index", "document", "synthetic_query"])
    negative_queries_df['Context_Relevance_Label'] = "No"

    synthetic_queries_filename = settings.get('synthetic_queries_filename', 'intermediate_queries.tsv')
    negative_queries_df.to_csv(synthetic_queries_filename, mode='a', header=not os.path.exists(synthetic_queries_filename), index=False, sep="\t")

    return negative_queries_df


def save_synthetic_queries(documents: pd.DataFrame, filename: str) -> None:
    """
    Saves the generated synthetic queries to a TSV file.

    Args:
        documents (pd.DataFrame): DataFrame containing the documents with the generated synthetic queries.
        filename (str): Filename to save the generated synthetic queries.
    """
    documents.to_csv(filename, index=False, sep="\t")
    print("Saved synthetic queries to: " + filename)

#ga1nang change
# def generate_synthetic_queries(documents: pd.DataFrame, settings: dict) -> pd.DataFrame:
#     """
#     Generate synthetic queries using the FLAN approach.

#     Args:
#         documents (pd.DataFrame): DataFrame containing the documents for which synthetic queries are to be generated.
#         settings (dict): Dictionary containing various settings and parameters required for generating synthetic queries.

#     Returns:
#         pd.DataFrame: DataFrame containing the documents with the generated synthetic queries.
#     """
#     # Print start message in a box
#     message = "Starting Synthetic Query Generation"
#     box_width = len(message) + 4
#     print("\n" + "=" * box_width)
#     print(f"| {message} |")
#     print("=" * box_width + "\n")

#     # Set configuration values
#     total_documents = len(documents)
#     initial_queries_per_document = 2 # Intended queries per document (not directly used here)
#     chunk_size = total_documents # Used when generating queries in batches
#     num_documents = len(documents)
#     half_num_documents = num_documents // 2
    
#     # If odd number of documents, increment first half
#     if num_documents % 2 != 0:
#         half_num_documents += 1
    
#     # # Split documents into two halves (not used later but possibly for ablation or debugging)
#     # first_half_documents = documents.head(half_num_documents)
#     # second_half_documents = documents.tail(num_documents - half_num_documents)
    
#      # Step 1: Generate initial set of positive synthetic queries
#     print(f"Generating positive queries for all {len(documents)} documents...")
#     positive_queries_df = generate_positive_synthetic_queries(documents, settings, chunk_size)
#     num_to_sample = len(documents)
    
#     # Step 2: Check if we have enough unique queries (at least 2 per document)
#     #ga1nang: change 2 to initial_queries_per_document
#     while len(positive_queries_df) < num_to_sample * initial_queries_per_document:
#         print("Warning: Not enough unique positive queries. Generating more...")
#         additional_queries = generate_positive_synthetic_queries(documents, settings, chunk_size)
#         positive_queries_df = pd.concat([positive_queries_df, additional_queries]).drop_duplicates(subset=['document', 'synthetic_query'])
    
#     # Step 3: Sample 1 positive query per document (Set 1)
#     positive_queries_set1 = positive_queries_df.groupby('document').apply(lambda x: x.sample(n=1, random_state=42)).reset_index(drop=True)
    
#     # Step 4: Sample another 1 positive query per document (Set 2), ensuring non-overlap with Set 1
#     remaining_queries = positive_queries_df[~positive_queries_df.apply(tuple, 1).isin(positive_queries_set1.apply(tuple, 1))]
#     positive_queries_set2 = remaining_queries.groupby('document').apply(lambda x: x.sample(n=1, random_state=43)).reset_index(drop=True)
    
#     # Mark context relevance label as "Yes" for both sets
#     positive_queries_set1['Context_Relevance_Label'] = 'Yes'
#     positive_queries_set2['Context_Relevance_Label'] = 'Yes'
    
#     # Step 5: Generate negative queries (queries not relevant to the given document)
#     print(f"Generating negative queries...")
#     negative_queries_df = generate_negative_synthetic_queries(positive_queries_df, documents, settings)
    
#     # Sample 1 negative query per document
#     negative_queries_df = negative_queries_df.groupby('document').apply(lambda x: x.sample(n=1, random_state=44)).reset_index(drop=True)
    
#     # Step 6: Combine all queries (positive and negative)
#     combined_queries_df = pd.concat([positive_queries_set1, positive_queries_set2, negative_queries_df], ignore_index=True)
    
#     # Save the combined result to disk
#     save_synthetic_queries(combined_queries_df, settings['synthetic_queries_filename'])

#     # Print end message
#     message = "Synthetic query generation completed."
#     box_width = len(message) + 4

#     print("\n" + "=" * box_width)
#     print(f"| {message} |")
#     print("=" * box_width + "\n")

#     # Final summary
#     print(f"Total queries saved: {len(combined_queries_df)} (Positive Set 1: {len(positive_queries_set1)}, Positive Set 2: {len(positive_queries_set2)}, Negative: {len(negative_queries_df)})")

#     return combined_queries_df

def generate_synthetic_queries(documents: list, settings: dict) -> pd.DataFrame:
    """
    Generate synthetic queries from a list of PDF documents (in bytes).

    Args:
        documents (list): List of PDF byte content (each item is a single document).
        settings (dict): Configuration for synthetic query generation.

    Returns:
        pd.DataFrame: A DataFrame of generated synthetic queries.
    """
    # Header
    message = "Starting Synthetic Query Generation"
    print("\n" + "=" * (len(message) + 4))
    print(f"| {message} |")
    print("=" * (len(message) + 4) + "\n")

    initial_queries_per_document = 2
    num_documents = len(documents)

    print(f"Generating positive queries for all {num_documents} documents...")

    # Step 1: Generate initial positive queries
    positive_queries = generate_positive_synthetic_queries(documents, settings).to_dict(orient="records")

    # Step 2: Ensure enough unique queries
    while len(positive_queries) < num_documents * initial_queries_per_document:
        print("Warning: Not enough unique positive queries. Generating more...")
        more_queries = generate_positive_synthetic_queries(documents, settings).to_dict(orient="records")
        positive_queries.extend(more_queries)

        # Drop duplicates (by document index and synthetic_query string)
        seen = set()
        filtered = []
        for q in positive_queries:
            key = (q['document_index'], q['synthetic_query'])
            if key not in seen:
                seen.add(key)
                filtered.append(q)
        positive_queries = filtered

    # Step 3: Sample 2 unique positives per document
    by_doc = {}
    for q in positive_queries:
        by_doc.setdefault(q['document_index'], []).append(q)

    set1, set2 = [], []
    for doc_idx, queries in by_doc.items():
        if len(queries) >= 2:
            set1.append(queries[0])
            set2.append(queries[1])
        elif len(queries) == 1:
            set1.append(queries[0])

    for q in set1 + set2:
        q['Context_Relevance_Label'] = 'Yes'

    # Step 4: Generate negative queries
    print("Generating negative queries...")
    all_positives_df = pd.DataFrame(set1 + set2)
    negative_queries = generate_negative_synthetic_queries(all_positives_df, documents, settings)

    # Step 5: Sample 1 negative per document
    by_doc_neg = {}
    for q in negative_queries.to_dict(orient="records"):
        by_doc_neg.setdefault(q['document_index'], []).append(q)
    sampled_negatives = [v[0] for v in by_doc_neg.values()]
    for q in sampled_negatives:
        q['Context_Relevance_Label'] = 'No'

    # Step 6: Combine and save
    all_queries = set1 + set2 + sampled_negatives
    combined_df = pd.DataFrame(all_queries)

    save_synthetic_queries(combined_df, settings['synthetic_queries_filename'])

    # Summary
    print("\n" + "=" * (len(message) + 4))
    print(f"| Synthetic query generation completed. |")
    print("=" * (len(message) + 4) + "\n")
    print(f"Total queries saved: {len(combined_df)} (Positive Set 1: {len(set1)}, Positive Set 2: {len(set2)}, Negative: {len(sampled_negatives)})")

    return combined_df

def generate_answers(synthetic_queries: pd.DataFrame, answer_generation_settings: dict) -> pd.DataFrame:
    """
    Generate synthetic answers using the FLAN approach.

    Args:
        synthetic_queries (pd.DataFrame): DataFrame containing the synthetic queries.
        answer_generation_settings (dict): Dictionary containing settings and parameters for answer generation.

    Returns:
        pd.DataFrame: DataFrame containing the synthetic queries with generated answers.
    """
    # Init client of gemini api
    load_dotenv()
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    # Start to generate synthetic answer
    if answer_generation_settings['api_model']:
        tqdm.pandas(desc=f"Generating answers... ({answer_generation_settings['model_name']})", total=synthetic_queries.shape[0])
        synthetic_queries["generated_answer"] = synthetic_queries.progress_apply(
            lambda x: generate_synthetic_answer_gemini_approach(
                x["document"], 
                x["synthetic_query"], 
                answer_generation_settings['synthetic_valid_answer_prompt'], 
                answer_generation_settings['answer_gen_few_shot_examples'], 
                answer_generation_settings['model_name'],  
                client
            ), 
            axis=1
        )
    else: 
        tqdm.pandas(desc="Generating answers... (FLAN)", total=synthetic_queries.shape[0])
        synthetic_queries["generated_answer"] = synthetic_queries.progress_apply(
            lambda x: generate_answer_llm_approach(
            x["document"], 
            x["synthetic_query"], 
            answer_generation_settings['answer_gen_few_shot_examples'], 
            answer_generation_settings['length_of_fewshot_prompt_answer_gen'], 
            answer_generation_settings['device'], 
            answer_generation_settings['tokenizer'], 
            answer_generation_settings['model'], 
            answer_generation_settings['for_fever_dataset'], 
            answer_generation_settings['for_wow_dataset']
        ), 
        axis=1
    )
    return synthetic_queries

def label_answers(synthetic_queries: pd.DataFrame) -> pd.DataFrame:
    """
    Label the generated answers for faithfulness and relevance.

    This function takes a DataFrame containing synthetic queries and their generated answers,
    and labels each answer for faithfulness and relevance. The labels are added as new columns
    in the DataFrame.

    Args:
        synthetic_queries (pd.DataFrame): DataFrame containing the synthetic queries and their generated answers.

    Returns:
        pd.DataFrame: DataFrame with additional columns for answer faithfulness and relevance labels.
    """
    
    # Label each generated answer for faithfulness
    synthetic_queries["Answer_Faithfulness_Label"] = [
        check_generated_answer(synthetic_queries.iloc[i]['generated_answer']) for i in range(len(synthetic_queries))
    ]
    
    # Label each generated answer for relevance
    synthetic_queries["Answer_Relevance_Label"] = [
        check_generated_answer(synthetic_queries.iloc[i]['generated_answer']) for i in range(len(synthetic_queries))
    ]
    
    return synthetic_queries

# def generate_contradictory_answers_wrapper(synthetic_queries: pd.DataFrame, answer_generation_settings: dict) -> pd.DataFrame:
#     """
#     Generate contradictory answers using the specified approach.

#     This function generates contradictory answers for the given synthetic queries based on the provided settings.

#     Args:
#         synthetic_queries (pd.DataFrame): DataFrame containing the synthetic queries.
#         answer_generation_settings (dict): Dictionary containing settings for answer generation, including:
#             - 'number_of_contradictory_answers_added_ratio' (float): Ratio to determine the number of contradictory answers to add.
#             - 'few_shot_examples_for_contradictory_answers' (list): Few-shot examples for generating contradictory answers (if applicable).
#             - 'device' (str): Device to use for model inference.
#             - 'tokenizer' (transformers.PreTrainedTokenizer): Tokenizer for the model.
#             - 'model' (transformers.PreTrainedModel): Model to use for generating answers.
#             - 'for_fever_dataset' (bool): Flag indicating if the dataset is for FEVER.
#             - 'for_wow_dataset' (bool): Flag indicating if the dataset is for WoW.

#     Returns:
#         pd.DataFrame: DataFrame with added contradictory answers.
#     """

#     synthetic_contradictory_answers = generate_contradictory_answer_examples(
#         synthetic_queries, 
#         int(len(synthetic_queries) * answer_generation_settings['number_of_contradictory_answers_added_ratio']), 
#         few_shot_examples_for_contradictory_answers=answer_generation_settings['few_shot_examples_for_contradictory_answers'], 
#         api_model=answer_generation_settings['api_model'],
#         synthetic_contradictory_answer_prompt=answer_generation_settings['synthetic_contradictory_answer_prompt'],
#         device=answer_generation_settings['device'], 
#         tokenizer=answer_generation_settings['tokenizer'], 
#         model=answer_generation_settings['model'], 
#         for_fever_dataset=answer_generation_settings['for_fever_dataset'], 
#         for_wow_dataset=answer_generation_settings['for_wow_dataset']
#     )
#     return synthetic_contradictory_answers

def process_embeddings(synthetic_queries: pd.DataFrame, answer_generation_settings: dict) -> pd.DataFrame:
    """
    Handle embedding generation and additional negatives/positives.

    This function processes the embeddings for the synthetic queries based on the provided settings.
    It generates an index, filters the synthetic queries, and adds additional negatives and positives.

    Args:
        synthetic_queries (pd.DataFrame): DataFrame containing the synthetic queries.
        answer_generation_settings (dict): Dictionary containing settings for answer generation, including:
            - 'regenerate_embeddings' (bool): Flag to determine if embeddings should be regenerated.
            - 'number_of_negatives_added_ratio' (float): Ratio to determine the number of additional negatives to add.
            - 'lower_bound_for_negatives' (int): Lower bound for the number of negatives.
            - 'number_of_positives_added_ratio' (float): Ratio to determine the number of additional positives to add.

    Returns:
        pd.DataFrame: DataFrame with processed embeddings and additional negatives/positives.
    """
    if answer_generation_settings['regenerate_embeddings']:
        print("Generating index and negatives!")
        documentation_index = generate_index(synthetic_queries)
        synthetic_queries = filter_synthetic_queries(synthetic_queries, documentation_index)
        synthetic_queries = generate_additional_negatives(
            synthetic_queries, 
            documentation_index, 
            answer_generation_settings['number_of_negatives_added_ratio'], 
            answer_generation_settings['lower_bound_for_negatives']
        )
        synthetic_queries = generate_additional_positives(
            synthetic_queries, 
            documentation_index, 
            answer_generation_settings['number_of_positives_added_ratio']
        )
    return synthetic_queries

def shuffle_and_save(synthetic_queries: pd.DataFrame, synthetic_queries_filename: str) -> None:
    """
    Shuffle and save the synthetic queries to a specified file.

    This function shuffles the rows of the synthetic queries DataFrame and saves the result to a file in TSV format.

    Args:
        synthetic_queries (pd.DataFrame): The DataFrame containing synthetic queries to be shuffled and saved.
        synthetic_queries_filename (str): The filename where the shuffled synthetic queries will be saved.

    Returns:
        None
    """
    # Ensure specific conditions for rows where Context_Relevance_Label is "No"
    condition = synthetic_queries['Context_Relevance_Label'] == "No"
    synthetic_queries.loc[condition, ['generated_answer', 'Answer_Relevance_Label', 'Answer_Faithfulness_Label']] = ""
    
    # Shuffle the synthetic queries DataFrame with a fixed random state for reproducibility
    synthetic_queries = synthetic_queries.sample(n=len(synthetic_queries), random_state=42)
    
    # Save the shuffled DataFrame to a TSV file without the index
    synthetic_queries.to_csv(synthetic_queries_filename, index=False, sep="\t")
    
    # Print completion messages
    print("Completed synthetic generation!")
    print(f"Saved synthetic queries file to: {synthetic_queries_filename}")

#ga1nang change
def generate_synthetic_answers(synthetic_queries_filename: str, answer_generation_settings: dict) -> None:
    """
    Main function to generate and save synthetic answers.

    This function reads synthetic queries from a file, processes them to generate answers,
    labels, and contradictory answers, and then saves the results back to the file. It also
    processes embeddings and shuffles the synthetic queries before saving.

    Args:
        synthetic_queries_filename (str): The filename where the synthetic queries are stored.
        answer_generation_settings (dict): Dictionary containing settings for answer generation, including:
            - 'regenerate_answers' (bool): Flag to determine if answers should be regenerated.
            - 'regenerate_embeddings' (bool): Flag to determine if embeddings should be regenerated.
            - 'number_of_negatives_added_ratio' (float): Ratio to determine the number of additional negatives to add.
            - 'lower_bound_for_negatives' (int): Lower bound for the number of negatives.
            - 'number_of_positives_added_ratio' (float): Ratio to determine the number of additional positives to add.

    Returns:
        None
    """
    # Read the synthetic queries from the specified file
    synth_queries = pd.read_csv(synthetic_queries_filename, sep="\t")
    
    # Drop any duplicated columns
    synth_queries = synth_queries.loc[:, ~synth_queries.columns.duplicated()]

    # Check if answers need to be regenerated
    if answer_generation_settings['regenerate_answers']:
        message = "Beginning answer generation!"
        box_width = len(message) + 4

        print("\n" + "=" * box_width)
        print(f"| {message} |")
        print("=" * box_width + "\n")
        
        # Determine the number of documents to process for answers
        total_queries = len(synth_queries)
        num_documents = total_queries // 3  # Since we have duplicated the first half
        half_num_documents = num_documents
        
        # Adjust for odd number of documents
        if num_documents % 2 != 0:
            half_num_documents += 1

        # Select first chunk queries for generating answers (excluding duplicates)
        first_half_queries = synth_queries.head(half_num_documents)

        print(f"Generating answers for {len(first_half_queries)} queries...")

        # Generate answers for the first chunk of the synthetic queries
        first_half_queries = generate_answers(first_half_queries, answer_generation_settings)
        
        # Label the generated answers
        first_half_queries = label_answers(first_half_queries)
        
        print(f"Generated answers for {len(first_half_queries)} queries.")

        # Ensure the columns 'generated_answer', 'Answer_Faithfulness_Label', and 'Answer_Relevance_Label' are correctly updated
        synth_queries.loc[first_half_queries.index, 'generated_answer'] = first_half_queries['generated_answer']
        synth_queries.loc[first_half_queries.index, 'Answer_Faithfulness_Label'] = first_half_queries['Answer_Faithfulness_Label']
        synth_queries.loc[first_half_queries.index, 'Answer_Relevance_Label'] = first_half_queries['Answer_Relevance_Label']
        
        # Save the synthetic queries with positive answers back to the file
        synth_queries.to_csv(synthetic_queries_filename, index=False, sep="\t")
        print(f"Saved positive answers to: {synthetic_queries_filename}")

        # Generate negative answers for the second chunk
        print("Generating negative answers for the second chunk of queries...")
        
        second_half_queries = synth_queries.iloc[half_num_documents:2 * half_num_documents].copy()
        sampled_answers = np.random.choice(first_half_queries['generated_answer'].values, size=len(second_half_queries), replace=False)
        second_half_queries['generated_answer'] = sampled_answers
        second_half_queries['Answer_Faithfulness_Label'] = "No"
        second_half_queries['Answer_Relevance_Label'] = "No"

        # Update the original dataframe with the generated negative answers
        synth_queries.loc[second_half_queries.index, 'generated_answer'] = second_half_queries['generated_answer']
        synth_queries.loc[second_half_queries.index, 'Answer_Faithfulness_Label'] = second_half_queries['Answer_Faithfulness_Label']
        synth_queries.loc[second_half_queries.index, 'Answer_Relevance_Label'] = second_half_queries['Answer_Relevance_Label']
        
        # Save the synthetic queries with answers back to the file
        synth_queries.to_csv(synthetic_queries_filename, index=False, sep="\t")
        print(f"Saved answers to: {synthetic_queries_filename}")

    # Re-read the synthetic queries from the file
    synth_queries = pd.read_csv(synthetic_queries_filename, sep="\t")
    
    # Check if modifications are needed for FEVER dataset formatting
    if answer_generation_settings['for_fever_dataset']:
        # Modify answers based on FEVER dataset needs
        fever_conditions = (synth_queries['Context_Relevance_Label'] == 'Yes') & (synth_queries['Answer_Relevance_Label'] == 'No')
        synth_queries.loc[fever_conditions, 'generated_answer'] = 'REFUTES'
        print("FEVER dataset formatting applied to synthetic answers.")
    
    # Shuffle and save the synthetic queries
    shuffle_and_save(synth_queries, synthetic_queries_filename)

    message = "Answer generation and processing completed."
    box_width = len(message) + 4

    print("\n" + "=" * box_width)
    print(f"| {message} |")
    print("=" * box_width + "\n")