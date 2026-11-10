"""Date context tools."""
import json
from datetime import datetime, timedelta
import calendar
from strands import tool
from .base_tool import BaseTool


class DateTools(BaseTool):
    """Provides date context and calculations."""
    
    def get_tools(self):
        return [self.get_current_date_context]
    
    @tool
    def get_current_date_context(self) -> str:
        """Get the current date and time context for accurate date calculations.
        
        Use this tool BEFORE making any date-based queries to ensure you're using
        the correct current date, not dates from your training data.
        
        Returns complete calendar periods (full months, full weeks) for accurate cost analysis.
        
        Returns:
            Current date information including year, month, and common date ranges
        """
        now = datetime.now()
        current_year = now.year
        current_month = now.month
        
        # Helper functions
        def first_day_of_month(year, month):
            return datetime(year, month, 1)
        
        def last_day_of_month(year, month):
            last_day = calendar.monthrange(year, month)[1]
            return datetime(year, month, last_day)
        
        def start_of_week(date):
            return date - timedelta(days=date.weekday())
        
        # Current month
        current_month_start = first_day_of_month(current_year, current_month)
        current_month_end = now
        
        # Last month (complete)
        if current_month == 1:
            last_month_year = current_year - 1
            last_month_num = 12
        else:
            last_month_year = current_year
            last_month_num = current_month - 1
        
        last_month_start = first_day_of_month(last_month_year, last_month_num)
        last_month_end = last_day_of_month(last_month_year, last_month_num)
        
        # Two months ago (complete)
        if current_month <= 2:
            two_months_ago_year = current_year - 1
            two_months_ago_num = 12 + current_month - 2
        else:
            two_months_ago_year = current_year
            two_months_ago_num = current_month - 2
        
        two_months_ago_start = first_day_of_month(two_months_ago_year, two_months_ago_num)
        two_months_ago_end = last_day_of_month(two_months_ago_year, two_months_ago_num)
        
        # Last 2 complete months
        last_2_months_start = two_months_ago_start
        last_2_months_end = last_month_end
        
        # Last 3 complete months
        if current_month <= 3:
            three_months_ago_year = current_year - 1
            three_months_ago_num = 12 + current_month - 3
        else:
            three_months_ago_year = current_year
            three_months_ago_num = current_month - 3
        
        three_months_ago_start = first_day_of_month(three_months_ago_year, three_months_ago_num)
        last_3_months_end = last_month_end
        
        # Week calculations
        current_week_start = start_of_week(now)
        current_week_end = now
        
        last_week_end = start_of_week(now) - timedelta(days=1)
        last_week_start = start_of_week(last_week_end)
        
        last_2_weeks_end = last_week_end
        last_2_weeks_start = last_week_start - timedelta(days=7)
        
        last_4_weeks_end = last_week_end
        last_4_weeks_start = last_week_start - timedelta(days=21)
        
        # Year calculations
        ytd_start = datetime(current_year, 1, 1)
        ytd_end = now
        
        last_year_start = datetime(current_year - 1, 1, 1)
        last_year_end = datetime(current_year - 1, 12, 31)
        
        context = {
            "current_date": now.strftime('%Y-%m-%d'),
            "current_year": current_year,
            "current_month_name": now.strftime('%B %Y'),
            "note": "All date ranges use COMPLETE calendar periods (full months, full weeks)",
            "current_month": {
                "start": current_month_start.strftime('%Y-%m-%d'),
                "end": current_month_end.strftime('%Y-%m-%d'),
                "description": f"Current month ({now.strftime('%B %Y')}) from start to today"
            },
            "last_month": {
                "start": last_month_start.strftime('%Y-%m-%d'),
                "end": last_month_end.strftime('%Y-%m-%d'),
                "description": f"Complete last month ({last_month_start.strftime('%B %Y')})"
            },
            "last_2_months": {
                "start": last_2_months_start.strftime('%Y-%m-%d'),
                "end": last_2_months_end.strftime('%Y-%m-%d'),
                "description": f"Last 2 complete months ({two_months_ago_start.strftime('%B')} + {last_month_start.strftime('%B %Y')})"
            },
            "last_3_months": {
                "start": three_months_ago_start.strftime('%Y-%m-%d'),
                "end": last_3_months_end.strftime('%Y-%m-%d'),
                "description": f"Last 3 complete months (ending {last_month_start.strftime('%B %Y')})"
            },
            "current_week": {
                "start": current_week_start.strftime('%Y-%m-%d'),
                "end": current_week_end.strftime('%Y-%m-%d'),
                "description": "Current week (Monday to today)"
            },
            "last_week": {
                "start": last_week_start.strftime('%Y-%m-%d'),
                "end": last_week_end.strftime('%Y-%m-%d'),
                "description": "Last complete week (Monday to Sunday)"
            },
            "last_2_weeks": {
                "start": last_2_weeks_start.strftime('%Y-%m-%d'),
                "end": last_2_weeks_end.strftime('%Y-%m-%d'),
                "description": "Last 2 complete weeks"
            },
            "last_4_weeks": {
                "start": last_4_weeks_start.strftime('%Y-%m-%d'),
                "end": last_4_weeks_end.strftime('%Y-%m-%d'),
                "description": "Last 4 complete weeks"
            },
            "year_to_date": {
                "start": ytd_start.strftime('%Y-%m-%d'),
                "end": ytd_end.strftime('%Y-%m-%d'),
                "description": f"Year to date ({current_year})"
            },
            "last_year": {
                "start": last_year_start.strftime('%Y-%m-%d'),
                "end": last_year_end.strftime('%Y-%m-%d'),
                "description": f"Complete last year ({current_year - 1})"
            },
            "usage_instructions": {
                "last_2_months": "Use 'last_2_months' range - includes 2 COMPLETE calendar months",
                "last_month": "Use 'last_month' range - includes 1 COMPLETE calendar month",
                "last_2_weeks": "Use 'last_2_weeks' range - includes 2 COMPLETE weeks (Mon-Sun)",
                "last_week": "Use 'last_week' range - includes 1 COMPLETE week (Mon-Sun)",
                "important": "ALWAYS use complete periods, not rolling days from today"
            }
        }
        
        return json.dumps(context, indent=2)
