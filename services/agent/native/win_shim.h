/* MinGW.org 老头文件缺 _wfopen 声明（ufbx.c 在 Windows 分支要用），
   不声明的话 64 位下 int->pointer 截断会直接崩。 */
#include <stdio.h>
#include <wchar.h>
#if defined(_WIN32) && !defined(_MSC_VER)
extern FILE *__cdecl _wfopen(const wchar_t *_Filename, const wchar_t *_Mode);
#endif
