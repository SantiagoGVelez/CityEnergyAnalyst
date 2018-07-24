import os
"""
=================
Config
=================
"""
if os.name == 'nt':  # Windows
    LOCATOR = 'C:/reference-case-WTP-reduced/WTP_MIX_m/'
    # LOCATOR = 'C:/reference-case-WTP/MIX_high_density/'
    THREADS = 0
elif os.name == 'posix':  # Linux
    LOCATOR = '/home/thanhphong.huynh/WTP_MIX_m/'
    # LOCATOR = '/home/thanhphong.huynh/MIX_high_density/'
    THREADS = 8
else:
    raise ValueError('No Linux or Windows OS!')
