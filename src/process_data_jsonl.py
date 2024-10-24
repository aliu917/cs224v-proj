"""
jsonl format:
{"id": "integer", "content": "string", "document_title": "string", "full_section_title": "string"}
- id: counter
- content: chunk text content to <= 500 tokens with https://python.langchain.com/v0.1/docs/modules/data_connection/document_transformers/
- document_title: chapter title
- full_section_title: chapter title > section title > subsection title > ...


Some notes from observing the jsons:
- document_title: taken from first text. We are manually extracting from one chapter at a time, so this becomes trivial.

- full_section_title: For the most part, these should be in the "section_header" labeled elements. However,
                      it doesn't seem like the json parse is very good at finding subsections. Ex: "Senegambia"
                      is a subsection of "Conquest and reaction in French West Africa ..." but they are both labeled
                      "section_header." Currently thinking a hacky method of getting this would be to use the "prov"
                      and roughly measure the text height (if a section is at least 1 pt smaller than another section
                      header, then it is a subsection or something). Tested this and I think the diff is enough to tell:

                      ** Senegambia:
                      "prov": [
                        {
                          "page_no": 4,
                          "bbox": {
                            "l": 39.72174835205078,
                            "t": 177.33309936523438,
                            "r": 98.95807647705078,
                            "b": 164.8936767578125,
                            "coord_origin": "BOTTOMLEFT"
                          },
                          "charspan": [
                            0,
                            10
                          ]
                        }
                      ]

                      ** Conquest and reaction in French West Africa ...
                      "prov": [
                        {
                          "page_no": 4,
                          "bbox": {
                            "l": 40.32535171508789,
                            "t": 479.55426025390625,
                            "r": 371.5114440917969,
                            "b": 464.8079833984375,
                            "coord_origin": "BOTTOMLEFT"
                          },
                          "charspan": [
                            0,
                            54
                          ]
                        }
                      ]

                    In each bbox, subtract t - b:

                    Conquest and reaction in French West Africa ...
                      479.55426025390625 - 464.8079833984375 = 14.74627685546875

                    Senegambia:
                      177.33309936523438 - 164.8936767578125 = 12.439422607421875

                    Also confirmed "Tukulor empire" (another subsection of Conquest and reaction in French West Africa):
                      323.73651123046875 - 311.451171875 = 12.28533935546875

                    >> was hacky. Had to fix some things but the final version works. See end for printed outline.

- content: not in scope for this file.

"""
from collections import OrderedDict
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pathlib import Path

import json
import os
import re
import tiktoken


class ResultObject:
    def __init__(self, id):
        self.full_section_list = []
        self.final_jsonl = []
        self.curr_title = ""
        self.curr_text = ""
        self.curr_idx = id


def create_full_sections(full_section_list):
    section_titles = [x[0] for x in full_section_list]
    return " > ".join(section_titles)


# Note: assumption of 55 char line length only applies to
# level 1 sections. Breaks on level 2 sections but is ok bc
# we don't have level 3 sections.
def calculate_section_height(json_section):
    text = json_section["text"]
    text_box = json_section["prov"][0]["bbox"]
    raw_height = float(text_box["t"]) - float(text_box["b"])
    text_length = len(text)
    estimate_lines = text_length // 55 + (text_length % 55 > 0)
    raw_estimate = raw_height / (1 + 0.9 * (estimate_lines-1))
    rounded = round(raw_estimate)
    return 14.5 if rounded >= 14 else raw_estimate


def compare_height(section_height1, section_height2):
    if section_height1 < section_height2 - 1.1:
        return "smaller"
    elif abs(section_height1 - section_height2) < 1.1:
        return "equal"
    else:
        return "larger"


def is_json_type(obj, label_type):
    return obj["label"] == label_type


def is_section_header(obj):
    return is_json_type(obj, "section_header")


def is_content(obj):
    return is_json_type(obj, "text")


def skip_footnote_text(content_text):

    if not any(char.isalpha() for char in content_text):
        return True

    # if len(content_text) < 200:
    number_pattern = r'^\d+\.\s'
    if re.match(number_pattern, content_text):
        return True

    # manual edge cases
    if "Sumatra see J. Bastin. op. cit., p. 89" in content_text:
        return True

    return False


def is_prev_continuing_text(text):
    for p in ['.', '?', '!']:
        if p in text[-2:] or (p in text[-3:] and not text[-1].isupper()):
            return False
    return True


def is_body_list_item(content_text):
    number_pattern = r'^\(\d+\)\s'
    if re.match(number_pattern, content_text):
        return True
    return False


def is_next_continuing_text(text):
    return not text[0].isupper()


def collect_fields(json_file, id):
    result = ResultObject(id)
    with open (json_file, 'r') as file:
        data = json.load(file)
        all_text = data["texts"]

        title = all_text[0]["text"]
        result.curr_title = title
        result.full_section_list.append((title, float("inf")))
        print(title)

        new_section = True

        # Skip second text (author)
        for i, section_json in enumerate(all_text[2:]):
            text = section_json["text"]

            # New text content
            if is_json_type(section_json, "text"):

                if skip_footnote_text(text):
                    continue

                separator = "\n"
                # Continuing paragraph text content
                if not new_section and len(result.final_jsonl) > 0 and (is_prev_continuing_text(result.curr_text) or is_next_continuing_text(text)):
                    separator = " "

                result.curr_text += separator + text
                new_section = False

            # New list item content
            elif is_json_type(section_json, "list_item"):
                if skip_footnote_text(text):
                    new_section = False
                    continue

                if not new_section and len(result.final_jsonl) > 0 and is_prev_continuing_text(result.curr_text) and is_body_list_item(text):
                    result.curr_text += " " + text
                    new_section = False
                    continue

            # New section header
            elif is_section_header(section_json):

                if skip_footnote_text(text):
                    new_section = False
                    continue

                new_section = True
                create_new_section(result)

                curr_section_height = result.full_section_list[-1][-1]
                new_section_height = calculate_section_height(section_json)

                # Edge case: section multi-line
                # if compare_height(new_section_height, curr_section_height) == "equal":
                if is_next_continuing_text(text) and is_section_header(all_text[2:][i-1]):
                    result.full_section_list[-1] = (result.full_section_list[-1][0] + " " + text, result.full_section_list[-1][1])
                    continue

                while not compare_height(new_section_height, curr_section_height) == "smaller" or len(result.full_section_list) == 3:  # TODO: update to soft compare
                    result.full_section_list.pop()
                    curr_section_height = result.full_section_list[-1][-1]

                if text == "Conclusion":
                    result.full_section_list = result.full_section_list[:1]

                indent = " > " * len(result.full_section_list)
                print(f"{indent} {new_section_height} - {text}")

                result.full_section_list.append((text, new_section_height))

        create_new_section(result)

    return result


def create_new_section(result):

    chunked_text = chunk_section(result.curr_text)
    for text_chunk in chunked_text:
        new_jsonl = dict()
        new_jsonl["id"] = result.curr_idx
        result.curr_idx += 1
        new_jsonl["content"] = text_chunk
        new_jsonl["document_title"] = result.curr_title
        new_jsonl["full_section_title"] = create_full_sections(result.full_section_list)
        result.final_jsonl.append(new_jsonl)

    result.curr_text = ""


def chunk_section(text):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        length_function=lambda x: len(tiktoken.get_encoding("o200k_base").encode(x)),
        is_separator_regex=False,
    )
    chunks = text_splitter.create_documents([text])
    return [chunk.page_content for chunk in chunks]


def process_data():

    all_results = []

    id = 0
    path = "../gha_texts/chapters"
    for filename in os.listdir(path):
        if filename.split('.')[-1] != "json":
            continue
        file_path = os.path.join(path, filename)
        print()
        print("processing ", str(file_path))
        print()
        result = collect_fields(file_path, id)
        id = result.curr_idx
        all_results += result.final_jsonl

    output_dir = Path("../gha_jsonl")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "final"
    with open(str(path) + ".jsonl", 'w') as file1, open(str(path) + "_chunked.json", 'w') as file2:
        wrapper = {"all" : []}
        for entry in all_results:
            od = OrderedDict([
                ("id", entry["id"]),
                ("document_title", entry["document_title"]),
                ("full_section_title", entry["full_section_title"]),
                ("content", entry["content"])
            ])
            file1.write(json.dumps(od) + '\n')
            wrapper["all"].append(od)
        file2.write(json.dumps(wrapper))


if __name__ == '__main__':
    process_data()

'''
Output: files and section structure


processing  ../gha_texts/chapters/Africa6_7.json

The British, Boers and Africans in South Africa, 1850-80
 >  14.5 - British withdrawal from the interior
 >  14.5 - The Cape Colony and Natal before 1 8 7 0
 >  14.5 - The Boer republics before 1870
 >  14.5 - Boer relations with the Africans before 1870
 >  14.5 - British expansion in South Africa 1870-80

processing  ../gha_texts/chapters/Africa6_24.json

States and peoples of Senegambia and Upper Guinea
 >  14.5 - Senegambia
 >  14.5 - Upper Guinea and Futa Jallon
 >  14.5 - The Kru bloc
 >  14.5 - The world of the southern Mande
 >  14.5 - Conclusion

processing  ../gha_texts/chapters/Africa6_28.json

The African diaspora
 >  14.5 - Introduction
 >  14.5 - The Middle East and south-east Asia 5
 >  14.5 - The diaspora in Europe
 >  14.5 - The western diaspora: background to the nineteenth century
 >  14.5 - The abolitionist period
 >  14.5 - The impact of Africa
 >  14.5 - The diaspora and Africa

processing  ../gha_texts/chapters/Africa7_8.json

African initiatives and resistance in Central Africa, 1880-1914
 >  14.5 - The struggle to maintain independence: the era of confrontation and alliance
 >  14.5 - Early localized resistance against colonial rule and capitalism
 >  14.5 - Colonial insurrections to 1918
 >  14.5 - Conclusion

processing  ../gha_texts/chapters/Africa7_9.json

African initiatives and resistance in Southern Africa
 >  14.5 - Southern Africa on the eve of colonial rule
 >  14.5 - The Zulu revolution and its aftermath
 >  14.5 - The missionary factor
 >  14.5 - Models of African initiatives and reactions
 >  14.5 - The Zulu, Ndebele, Bemba and Yao: the politics of confrontation
 >  >  12.369903564453125 - The Zulu
 >  >  12.050865173339844 - The Ndebele
 >  14.5 - The Ngwato, Lozi, Sotho, Tswana and Swazi initiatives and reaction: the model of protectorate or wardship
 >  >  11.923095703125 - The T s w a n a
 >  >  11.856109619140625 - The Swazi
 >  14.5 - The Hlubi, Mpondomise, Bhaca, Senga, Njanja, Shona, Tonga, Tawara, etc., initiatives and reactions: the model of alliance
 >  14.5 - African initiatives and reactions, 1895-1914
 >  >  12.10943603515625 - The Ndebele-Shona Chimurenga
 >  >  12.077392578125 - The Herero
 >  14.5 - Conclusion

processing  ../gha_texts/chapters/Africa6_25.json

States and peoples of the Niger Bend and the Volta
 >  14.5 - Political and institutional upheavals
 >  >  12.1927490234375 - The Mossi states
 >  >  13.339111328125 - The western and southern Volta plateaux
 >  >  12.236083984375 - Other peoples
 >  >  12.51580810546875 - The eastern regions of the Volta plateaux
 >  >  12.442047119140625 - The Bambara kingdoms of Segu and Kaarta
 >  >  12.6602783203125 - Summary
 >  14.5 - Socio-economic tensions
 >  >  12.3076171875 - Production and trade
 >  >  12.02056884765625 - Trade channels
 >  >  12.55743408203125 - Social change
 >  14.5 - Religious change
 >  11.321441650390625 - Conclusion

processing  ../gha_texts/chapters/Africa7_6.json

African initiatives and resistance in West Africa, 1880-1914
 >  14.5 - Conquest and reaction in French West Africa, 1880-1900
 >  >  12.439422607421875 - Senegambia
 >  >  12.28533935546875 - Tukulor empire
 >  >  12.1712646484375 - Samori and the French
 >  >  12.329986572265625 - Dahomey
 >  >  11.8634033203125 - The Baule and the French
 >  14.5 - Conquest and reaction in British West Africa,
 >  >  12.417999267578125 - Asante (Gold Coast)
 >  >  13.242095947265625 - Southern Nigeria
 >  >  12.636474609375 - Conquest and Reaction in Northern Nigeria
 >  14.5 - African Reactions and Responses in West Africa,
 >  >  11.887130737304688 - The rebellion of Mamadou Lamine
 >  >  11.713958740234375 - The Hut T a x rebellion
 >  >  12.16925048828125 - The Yaa Asantewaa War
 >  >  12.06976318359375 - Mass Migration
 >  >  11.87774658203125 - Strikes
 >  >  12.4046630859375 - Ideological protest
 >  >  12.4244384765625 - Elite associations
 >  14.5 - The causes of failure

processing  ../gha_texts/chapters/Africa6_26.json

Dahomey, Yorubaland, Borgu and Benin in the nineteenth century
 >  14.5 - The Mono-Niger area as the unit of analysis
 >  14.5 - The collapse of Old Oyó
 >  14.5 - The decline of the Benin kingdom
 >  14.5 - The growth of European interest
 >  14.5 - Socio-economic change and institutional adaptation

processing  ../gha_texts/chapters/Africa6_10.json

The East African coast and hinterland, 1845-80
 >  14.5 - Omani penetration and the expansion of trade
 >  >  12.194900512695312 - The Kilwa hinterland routes
 >  >  12.020416259765625 - The central Tanzanian routes
 >  >  12.27166748046875 - The Pangani valley route
 >  >  12.632049560546875 - The Mombasa hinterland routes
 >  >  6.693532843338816 - The effects of long-distance trade on East African societies
 >  14.5 - The Nguni invasion
 >  14.5 - The Maasai
 >  14.5 - Increased European pressures

processing  ../gha_texts/chapters/Africa7_11.json

Liberia and Ethiopia, 1880-1914: the survival of two A f r i c a n states
 >  14.5 - Liberia and Ethiopia on the eve of the Scramble for Africa
 >  >  11.59478759765625 - Liberia
 >  >  12.79644775390625 - Ethiopia
 >  14.5 - European aggression on Liberian and Ethiopian territory, 1880-1914
 >  >  12.707275390625 - Liberia
 >  >  12.2620849609375 - Ethiopia
 >  14.5 - Economic and social developments and European intervention in Liberia's and Ethiopia's internal affairs, 1880-1914
 >  >  12.45562744140625 - Liberia
 >  >  12.188232421875 - Ethiopia
 >  14.5 - The o u t c o m e of the S c r a m b l e and partition for L i b e r i a and Ethiopia

processing  ../gha_texts/chapters/Africa7_10.json

Madagascar, 1880S-1930S: African initiatives and reaction to colonial conquest and domination
 >  14.5 - A country divided in the face of the imperialist threat
 >  >  6.448990671258224 - The situation on the eve of thefirst F r a n c o - M e r i n a war 3
 >  >  12.759765625 - The isolation of the Malagasy rulers, 1882-94
 >  >  6.688907020970395 - The 'Kingdom of Madagascar' in 1894: weakness and disarray
 >  14.5 - A country offering uncoordinated resistance to colonial conquest
 >  >  12.43145751953125 - The failure of leadership
 >  >  12.02569580078125 - The Menalamba m o v e m n t s in Imerina
 >  >  6.479106702302632 - Popular opposition in the regions subject to the royal authority
 >  >  12.50042724609375 - The resistance of the independent peoples
 >  14.5 - A c o u n t r y united by its submission to France and its opposition to colonial domination
 >  >  6.600116930509869 - From colonization to the dawning of the national movement
 >  >  6.720099198190789 - The first reactions in opposition to the colonial system
 >  >  12.780792236328125 - Struggles to recover dignity
 >  14.5 - Conclusion

processing  ../gha_texts/chapters/Africa6_27.json

The Niger delta and the Cameroon region
 >  14.5 - Introduction
 >  14.5 - The Niger delta
 >  >  11.93194580078125 - The western delta
 >  >  11.8062744140625 - The eastern delta
 >  >  12.3785400390625 - The Igbo hinterland
 >  14.5 - The Cross river basin
 >  >  12.41851806640625 - The obong of Calabar
 >  >  12.098602294921875 - The Ekpe society and the Bloodmen
 >  14.5 - The Cameroon coast and its hinterland 14
 >  >  12.919891357421875 - The Ogowe basin and surrounding regions 23
 >  14.5 - Conclusion

processing  ../gha_texts/chapters/Africa7_7.json

African initiatives a n d resistance in East Africa, i880-1914
 >  14.5 - The European Scramble for East Africa and the patterns of African resistance
 >  >  12.69073486328125 - The response in Kenya
 >  >  12.308502197265625 - The r e s p o n s e in Tanganyika
 >  >  12.3656005859375 - The response in Uganda
 >  14.5 - East Africa under colonial rule
 >  >  12.95013427734375 - Anti-colonial movements in East Africa to 1914
'''