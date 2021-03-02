import numpy as np
import scipy.stats as st

from util import p2stars

WELCH = True

# returns t, p, na, nb
def ttest_rel(a, b, msg=None, min_n=2): return ttest(a, b, True, msg, min_n)
def ttest_ind(a, b, msg=None, min_n=2): return ttest(a, b, False, msg, min_n)

def ttest(a, b, paired, msg=None, min_n=2):
  if paired:
    abFinite = np.isfinite(a) & np.isfinite(b)
  a, b = (x[abFinite if paired else np.isfinite(x)] for x in (a, b))
  na, nb = len(a), len(b)
  if min(na, nb) < min_n:
    return np.nan, np.nan, na, nb
  with np.errstate(all='ignore'):
    t, p = st.ttest_rel(a, b) if paired else st.ttest_ind(a, b,
      equal_var=not WELCH)
  if msg:
    print("%spaired t-test -- %s:" %("" if paired else "un", msg))
    print("  n = %s means: %.3g, %.3g; t-test: p = %.5f, t = %.3f" %(
      "%d," %na if paired else "%d, %d;" %(na, nb),
      np.mean(a), np.mean(b), p, t))
  return t, p, na, nb

a = np.array([
0.123,
0.17,
0.165,
0.15,
0.167,
0.144,
0.137,
0.123,
0.156,
0.126,

])

b = np.array([
0.155,
0.163,
0.17,
0.172,
0.166,
0.131,
0.14,
])

res = ttest_ind(a, b, msg=True)
print('result (p value and stars): %.3f; %s' %(res[1], p2stars(res[1])))
