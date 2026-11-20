from .base_handler import BaseHandler, detect_ats, detect_ats_from_url, get_handler
from .workday_handler import WorkdayHandler
from .oracle_handler import OracleHandler
from .successfactors_handler import SuccessFactorsHandler
from .taleo_handler import TaleoHandler

__all__ = ["BaseHandler", "WorkdayHandler", "OracleHandler", "SuccessFactorsHandler", "TaleoHandler",
           "get_handler", "detect_ats", "detect_ats_from_url"]
