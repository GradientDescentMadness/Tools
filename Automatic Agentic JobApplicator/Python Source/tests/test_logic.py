"""
Offline unit tests for matching, field resolution and QA logic (no browser).
Run from 'Python Source':  python -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.field_resolver import Answer, FieldInfo, FieldResolver  # noqa: E402
from src.form_filler import FormField, FormFiller, section_from_headings  # noqa: E402
from src.matching import best_option, format_date, has_word, parse_date  # noqa: E402
from src.question_answerer import LocationAwareAnswerer, find_countries  # noqa: E402
from src.utils import load_config  # noqa: E402

RESUME = load_config(str(ROOT / "tests" / "regression" / "resume_data.yaml"))
QA = load_config(str(ROOT / "config" / "qa_rules.yaml"))


def resolver(location="Bangalore, Karnataka, India", url=""):
    return FieldResolver(RESUME, LocationAwareAnswerer(QA, RESUME, location, url))


def value(label, field_type="text", options=None, section="", card=0, location="Bangalore, India", placeholder=""):
    ans = resolver(location).resolve(FieldInfo(label, field_type, options or [], section, card, placeholder))
    return None if ans is None else (ans.date or ans.check if ans.date or (ans.check is not None and not ans.value) else ans.value)


class MatchingTests(unittest.TestCase):
    def test_whole_words(self):
        self.assertFalse(has_word("Ethnicity", "city"))
        self.assertFalse(has_word("Experience with Adobe", "dob"))
        self.assertTrue(has_word("City / Town", "city"))

    def test_degree_options_prefer_specific(self):
        opts = ["Bachelor of Arts", "Bachelor of Technology", "Master of Arts", "Master of Science", "MBA", "PhD"]
        cands = [RESUME["education"][0]["degree"]] + RESUME["education"][0]["degree_aliases"]
        self.assertEqual(best_option(opts, cands), "Master of Science")
        mba = [RESUME["education"][1]["degree"]] + RESUME["education"][1]["degree_aliases"]
        self.assertEqual(best_option(opts, mba), "MBA")
        btech = [RESUME["education"][2]["degree"]] + RESUME["education"][2]["degree_aliases"]
        self.assertEqual(best_option(opts, btech), "Bachelor of Technology")

    def test_yes_no_not_substring(self):
        self.assertEqual(best_option(["None of the above", "No", "Yes"], ["No"]), "No")
        self.assertEqual(best_option(["Yes, I am authorized", "No, I am not"], ["Yes"]), "Yes, I am authorized")

    def test_partial_prefers_most_specific_option(self):
        opts = ["Asian - Other", "Asian - Indian / Pakistani / Bangladeshi", "White", "Prefer not to say"]
        self.assertEqual(best_option(opts, ["Asian", "Asian Indian", "South Asian", "Indian"]),
                         "Asian - Indian / Pakistani / Bangladeshi")
        self.assertEqual(best_option(["British Indian Ocean Territory", "India (+91)"], ["India"]), "India (+91)")

    def test_codes_never_match_loosely(self):
        page_one = ["No Selection", "+1", "+20", "+211", "+212", "+291", "+297", "+298"]
        self.assertIsNone(best_option(page_one, ["+91", "India (+91)", "91", "India"]))
        self.assertEqual(best_option(page_one + ["+91"], ["+91", "India (+91)"]), "+91")

    def test_country_code_and_fallbacks(self):
        self.assertEqual(best_option(["- Select -", "Indonesia (+62)", "India (+91)"], ["+91", "India (+91)"]), "India (+91)")
        self.assertEqual(best_option(["Accounting", "Computer Science", "Industrial Engineering"],
                                     ["Production Engineering"], ["Industrial Engineering", "Mechanical Engineering"]),
                         "Industrial Engineering")

    def test_dates(self):
        self.assertEqual(parse_date("01/04/2022"), date(2022, 4, 1))
        self.assertIsNone(parse_date("Present"))
        self.assertEqual(format_date(date(2022, 4, 1), "MM/YYYY"), "04/2022")
        self.assertEqual(format_date(date(2022, 4, 1), "", "dd/mm/yyyy"), "01/04/2022")


class ResolverTests(unittest.TestCase):
    def test_previous_misfires_are_fixed(self):
        self.assertNotEqual(value("Ethnicity", "select"), RESUME["personal"]["address"]["city"])
        self.assertNotIn(value("Languages spoken"), [RESUME["eeoc_and_diversity"]["age_range"]])
        self.assertIsNone(value("Cover message", "textarea"))
        self.assertNotEqual(value("Excellent communication skills - describe", "textarea"), RESUME["personal"]["phone"])
        self.assertIsNone(value("Years of experience with Adobe Analytics"))
        self.assertEqual(value("Title", "select", ["Mr.", "Mrs.", "Ms.", "Dr."]), RESUME["personal"]["salutation"])

    def test_cards_map_to_nth_entry(self):
        self.assertEqual(value("Company", section="experience", card=1), RESUME["experience"][1]["company"])
        self.assertEqual(value("Job Title", section="experience", card=2), RESUME["experience"][2]["title"])
        self.assertEqual(value("School or University", section="education", card=1), RESUME["education"][1]["university"])

    def test_start_date_is_never_notice(self):
        v = value("Start Date", section="experience", card=0)
        self.assertEqual(v, parse_date(RESUME["experience"][0]["start_date"]))
        self.assertIsNone(value("Notice Period Date", "date"))
        self.assertEqual(value("Notice period"), RESUME["additional"]["notice_period"])

    def test_education_fields(self):
        self.assertEqual(value("Level of qualification", "select", section="education", card=2),
                         RESUME["education"][2]["qualification_level"])
        self.assertEqual(value("Qualification Status", "select", section="education"), "Completed")
        self.assertEqual(value("Commencement Date", section="education", card=1),
                         parse_date(RESUME["education"][1]["commencement_date"]))

    def test_personal_fields(self):
        r = resolver()
        r.has_country_code_field = True
        phone = r.resolve(FieldInfo("Phone Number")).value
        self.assertEqual(phone, "9000000001")
        self.assertEqual(r.resolve(FieldInfo("Country/Region Code", "select")).value, "+91")
        self.assertEqual(value("Post Code"), RESUME["personal"]["address"]["zip_code"])
        self.assertEqual(value("State/Province"), RESUME["personal"]["address"]["state"])
        self.assertEqual(value("Gender", "select"), RESUME["personal"]["gender"])
        self.assertEqual(value("Date of Birth"), parse_date(RESUME["personal"]["date_of_birth"]))
        self.assertEqual(value("Pronouns", "select"), RESUME["personal"]["pronouns"])

    def test_current_role_and_industry(self):
        self.assertEqual(value("Current Role?", "select", section="experience", card=0), "Yes")
        self.assertEqual(value("Current Role?", "select", section="experience", card=1), "No")
        self.assertEqual(value("Industry", "select", section="experience", card=0), RESUME["experience"][0]["industry"])


class WorkRowPicklistTests(unittest.TestCase):
    """HSBC-style work/education rows: 'Type of Business' and 'Country/Territory' picklists."""
    PROFILE = {
        "experience": [
            {"company": "Northwind Bank", "location": "Wilmington, DE, USA / Pune, India", "industry": "Banking / Financial Services"},
            {"company": "Contoso Consulting", "location": "Pune, India", "industry": "Information Technology / Consulting"},
            {"company": "Fabrikam Motors", "location": "Wilmington, DE, USA", "country": "India", "industry": "Automotive / Manufacturing"},
            {"company": "Somewhere", "location": "Remote"},
        ],
        "education": [{"university": "Example State University", "location": "Springfield, IL, US"}],
    }
    COUNTRIES = ["No Selection", "India", "Indonesia", "United States Minor Outlying Islands",
                 "United States of America", "British Indian Ocean Territory"]
    BUSINESS = ["No Selection", "Banking and Financial Services", "Consulting", "Information Technology", "Manufacturing", "Other"]

    def pick(self, label, card, options, section="experience"):
        ans = FieldResolver(self.PROFILE).resolve(FieldInfo(label, "select", options, section, card))
        return best_option(options, ans.candidates, ans.fallbacks) if ans else None

    def test_country_of_each_job(self):
        self.assertEqual(self.pick("Country/Territory", 0, self.COUNTRIES), "United States of America", "first place listed")
        self.assertEqual(self.pick("Country/Territory", 1, self.COUNTRIES), "India")
        self.assertEqual(self.pick("Country/Territory", 2, self.COUNTRIES), "India", "an explicit country wins")
        self.assertIsNone(self.pick("Country/Territory", 3, self.COUNTRIES), "unknown country is left empty")
        self.assertEqual(self.pick("Country", 0, self.COUNTRIES, "education"), "United States of America")

    def test_type_of_business(self):
        self.assertEqual(self.pick("Type of Business", 0, self.BUSINESS), "Banking and Financial Services")
        self.assertEqual(self.pick("Type of Business", 1, self.BUSINESS), "Information Technology")
        self.assertEqual(self.pick("Type of Business", 2, self.BUSINESS), "Manufacturing")
        self.assertEqual(self.pick("Line of business", 0, ["Retail", "Financial Services"]), "Financial Services")
        self.assertEqual(self.pick("Type of Business", 0, ["Retail", "Finance", "Other"]), "Finance", "nearest proxy")
        self.assertIsNone(self.pick("Type of Business", 3, self.BUSINESS), "no industry in the profile")


class MoodysEducationRowTests(unittest.TestCase):
    """Moody's 'Formal Education' rows: where-labels name the school, and 'Highest Level' is Yes/No."""

    def test_where_labels_are_not_the_school(self):
        self.assertEqual(value("Country of School", "select", WorkRowPicklistTests.COUNTRIES, "education", 0), "United States")
        self.assertEqual(value("State/City/Region of School", section="education", card=0), "Ann Arbor, MI")
        self.assertEqual(value("State/City/Region of School", section="education", card=1), "Ahmedabad")
        self.assertEqual(value("School (Please use the full name, for example, University of Connecticut)",
                               section="education", card=1), RESUME["education"][1]["university"])
        self.assertEqual(value("Company Location", section="experience", card=0), RESUME["experience"][0]["location"])

    def test_highest_level_yes_no(self):
        r = resolver()
        for card, expected in ((0, "Yes"), (1, "No"), (2, "No")):  # M.S. 2021 beats MBA 2014 (same level, more recent)
            ans = r.resolve(FieldInfo("Highest Level of Education", "select", [], "education", card))
            self.assertEqual(best_option(["No Selection", "Yes", "No"], ans.candidates, ans.fallbacks), expected)
        levels = ["Bachelor's Degree", "Master's Degree", "Doctorate"]
        ans = r.resolve(FieldInfo("Highest Level of Education", "select", levels, "education", 2))
        self.assertEqual(best_option(levels, ans.candidates, ans.fallbacks), "Bachelor's Degree")


class MoodysQuestionTests(unittest.TestCase):
    REL = ("Do you have a Personal Relationship with a current employee of Moody's or one of its affiliates or "
           "subsidiaries? Individuals in a Personal Relationship are defined as follows: relatives (by blood or "
           "through marriage, domestic partnership, or adoption); spouses; domestic partners.")

    def test_relationship_block(self):
        self.assertEqual(value(self.REL, "select", ["No Selection", "Yes", "No"]), "No")
        self.assertEqual(value("If you selected 'Yes' to the previous question, what is the name of the employee with "
                               "whom you have a Personal Relationship? Otherwise, enter N/A."), "N/A")
        self.assertIsNone(value("If yes, please provide details of your notice period"))
        nature = resolver().resolve(FieldInfo("What is the nature of the Personal Relationship you have with the "
                                              "individual identified? If there is no Personal Relationship, select not applicable.", "select"))
        self.assertEqual(best_option(["No Selection", "Family", "Spouse", "Not Applicable"], nature.candidates, nature.fallbacks),
                         "Not Applicable")
        self.assertEqual(value("What is your communication preference?", "select"), "Email")


def row_field(label, row, heading="Formal Education", value="", section=""):
    return FormField(element=None, field_type="text", label=label, headings=[heading], row=row, value=value, section=section)


class RowCardTests(unittest.TestCase):
    def test_rows_beat_label_counting(self):
        # the first (pre-filled) row has no 'From Date' box: label counting would give row 2's date card 0
        fields = [row_field("School", 4), row_field("Major", 4),
                  row_field("From Date", 9), row_field("School", 9), row_field("Major", 9)]
        FormFiller._assign_cards(fields)
        self.assertEqual([f.card for f in fields], [0, 0, 1, 1, 1])

    def test_leading_row_without_remove_control(self):
        fields = [row_field("School", -1), row_field("Major", -1),
                  row_field("School", 3), row_field("Major", 3), row_field("School", 8)]
        FormFiller._assign_cards(fields)
        self.assertEqual([f.card for f in fields], [0, 0, 1, 1, 2])

    def test_earlier_mixups_are_replaced_typed_values_kept(self):
        filler = FormFiller(None, RESUME, None, None)
        mba_school, ans = RESUME["education"][1]["university"], Answer("Chennai")
        self.assertTrue(filler._misplaced(row_field("State/City/Region of School", 2, value=mba_school, section="education"), ans))
        self.assertFalse(filler._misplaced(row_field("State/City/Region of School", 2, value="Chennai, Tamil Nadu", section="education"), ans))
        start = Answer("01/08/2007", date=date(2007, 8, 1))
        f = row_field("From Date", 2, value="07/01/2012", section="education")  # the MBA's start, shown MM/DD
        self.assertTrue(filler._misplaced(f, start))
        f.value = "08/01/2007"
        self.assertFalse(filler._misplaced(f, start))


class LocationTests(unittest.TestCase):
    def ask(self, q, location, url=""):
        return LocationAwareAnswerer(QA, RESUME, location, url).get_answer(q)

    def test_job_country_detection(self):
        self.assertEqual(find_countries("Bengaluru, Karnataka"), ["India"])
        self.assertEqual(find_countries("https://citi.wd5.myworkdayjobs.com/en-US/2/job/Bangalore-Karnataka-India/x"), ["India"])
        self.assertEqual(find_countries("New York, NY"), ["United States"])
        self.assertEqual(find_countries("Would you work for us?"), [])

    def test_sponsorship_and_authorization(self):
        self.assertEqual(self.ask("Will you require sponsorship to work in this role?", "Bengaluru, Karnataka"), "No")
        self.assertEqual(self.ask("Do you now or will you in the future require visa sponsorship?", "New York, NY, United States"), "Yes")
        self.assertEqual(self.ask("Will you now or in the future require immigration visa sponsorship?", "London, UK"), "Yes")
        self.assertEqual(self.ask("Is your work authorization status valid? Are you authorized to work here?", "Bangalore, India"), "Yes")
        self.assertEqual(self.ask("Are you legally authorized to work in Australia?", "Bangalore, India"), "No")
        self.assertEqual(self.ask("Are you legally authorized to work in India?", "Unknown Location"), "Yes")

    def test_unknown_location_is_not_guessed(self):
        self.assertIsNone(self.ask("Are you legally authorized to work in the country where this job is located?", "Unknown Location"))
        asked = LocationAwareAnswerer(QA, RESUME, "", "", ask=lambda q: "India")
        self.assertEqual(asked.get_answer("Do you require sponsorship?"), "No")

    def test_citizenship_questions(self):
        self.assertEqual(self.ask("Are you an Indian Citizen", "Bangalore, India"), "Yes")
        self.assertEqual(self.ask("Are you a British citizen?", "Bangalore, India"), "No")
        self.assertEqual(self.ask("Can you please confirm whether you are a USA Citizen or Green Card Holder?", "Bangalore"), "No")

    def test_rule_patterns_whole_word(self):
        self.assertIsNone(self.ask("Experience with trace / log analysis tools", "Bangalore, India"))


class PortalWordingTests(unittest.TestCase):
    """Labels seen on live Taleo / SuccessFactors / Oracle forms during the regression runs."""

    def test_sentences_are_not_address_fields(self):
        self.assertIsNone(value("Please state the source of information if choose Others in the question above"))

    def test_other_script_name_and_employer(self):
        self.assertEqual(value("Full Name in Chinese (if no Chinese name, please input NA)"), "NA")
        self.assertEqual(value("Present Office / Hospital / Institution"), RESUME["experience"][0]["company"])
        self.assertEqual(value("Number of months' pay per year"), "12")

    def test_conditional_followups_are_left(self):
        self.assertIsNone(value("If yes, please provide your employee ID"))

    def test_citizen_style_eligibility_options(self):
        from src.matching import best_option
        ans = resolver("Bengaluru").resolve(FieldInfo("Please confirm your eligibility to work in India", "text"))
        self.assertEqual(best_option(["No Selection", "Indian Citizen", "None of the above", "OCI (Overseas Citizen of India)"],
                                     ans.candidates, ans.fallbacks), "Indian Citizen")

    def test_degree_equivalents(self):
        from src.matching import degree_fallbacks
        opts = ["Select One", "M.Eng", "B.Eng", "GED", "High School", "Master's Degree", "MBA"]
        self.assertEqual(best_option(opts, ["B. Tech."], degree_fallbacks("B. Tech.", "Bachelor's Degree")), "B.Eng")
        self.assertEqual(best_option(opts, ["M.S. in Analytics (AI/ML)"], degree_fallbacks("M.S.", "Master's Degree")), "Master's Degree")

    def test_field_of_study_prefers_stated_proxy(self):
        ans = resolver().resolve(FieldInfo("Field of Study", "select", section="education", card=0))
        opts = ["Artificial Intelligence and Robotics", "Computer Science", "Data Science", "Accounting"]
        self.assertEqual(best_option(opts, ans.candidates, ans.fallbacks, proxies_before_partial=ans.proxies_first), "Data Science")

    def test_split_date_dropdowns_share_label(self):
        mk = lambda label, opts: FormField(element=None, field_type="select", label=label, options=opts)
        days, months, years = ["Day"] + [str(i) for i in range(1, 32)], ["Month"] + [f"{m}" for m in
            ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]], \
            ["Year"] + [str(y) for y in range(1940, 2010)]
        fields = [mk("Date of Birth", days), mk("", months), mk("", years)]
        FormFiller._group_split_dates(fields)
        self.assertEqual([(f.label, f.date_part) for f in fields],
                         [("Date of Birth", "day"), ("Date of Birth", "month"), ("Date of Birth", "year")])


class TabMatchingTests(unittest.TestCase):
    """--attach picks the open tab that belongs to the supplied link, even after portal redirects."""

    def test_scores(self):
        from src.attach import score_tab
        hsbc = "https://career2.successfactors.eu/careers?company=hsbcholdin&career_ns=job_application&career_job_req_id=64895&lang=en_GB"
        wd = "https://citi.wd5.myworkdayjobs.com/2/job/Bangalore-Karnataka-India/Director---AIM-USCC-Innovation-and-AI-Lead_26982863"
        self.assertEqual(score_tab(hsbc, hsbc), 100)
        self.assertGreaterEqual(score_tab(hsbc, "https://career2.successfactors.eu/career?company=hsbcholdin&career_job_req_id=64895&_s.crb=x"), 40)
        self.assertGreater(score_tab(hsbc, "https://career2.successfactors.eu/career?company=hsbcholdin&career_job_req_id=64895"),
                           score_tab(hsbc, "https://career2.successfactors.eu/career?company=hsbcholdin&career_job_req_id=11111"))
        self.assertGreaterEqual(score_tab(wd, "https://citi.wd5.myworkdayjobs.com/en-US/2/job/Bangalore-Karnataka-India/"
                                              "Director---AIM-USCC-Innovation-and-AI-Lead_26982863/apply/applyManually"), 40)
        self.assertLess(score_tab(hsbc, "https://career10.successfactors.com/careers?company=anzbanking"), 40)
        self.assertEqual(score_tab(hsbc, "https://www.google.com/search?q=hsbc"), 0)
        self.assertGreaterEqual(score_tab("hsbc", "https://career2.successfactors.eu/careers?company=hsbcholdin"), 40)
        self.assertEqual(score_tab(hsbc, "chrome://newtab/"), 0)


class CardAssignmentTests(unittest.TestCase):
    def test_numbered_headings_and_repetition(self):
        self.assertEqual(section_from_headings(["Work Experience 2", "My Experience"]), ("experience", 1))
        mk = lambda label, heads: FormField(element=None, field_type="text", label=label, headings=heads)
        fields = [mk("Job Title", ["Work Experience"]), mk("Company", ["Work Experience"]), mk("From", ["Work Experience"]),
                  mk("Job Title", ["Work Experience"]), mk("Company", ["Work Experience"]),
                  mk("School", ["Education"]), mk("Degree", ["Education"]), mk("School", ["Education"])]
        FormFiller._assign_cards(fields)
        self.assertEqual([(f.section, f.card) for f in fields],
                         [("experience", 0), ("experience", 0), ("experience", 0), ("experience", 1), ("experience", 1),
                          ("education", 0), ("education", 0), ("education", 1)])


if __name__ == "__main__":
    unittest.main()
