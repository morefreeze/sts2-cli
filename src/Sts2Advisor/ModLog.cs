// src/Sts2Advisor/ModLog.cs
namespace Sts2Advisor;

/// <summary>&lt;game user dir&gt;/Sts2Advisor/Sts2Advisor.log, plus the game's own log.</summary>
internal static class ModLog
{
    private static readonly object Gate = new();
    private static string _path = "";

    public static void Init(string dir)
    {
        Directory.CreateDirectory(dir);
        _path = Path.Combine(dir, "Sts2Advisor.log");
    }

    public static void Info(string message) => Write("INFO", message);

    public static void Error(string message) => Write("ERROR", message);

    private static void Write(string level, string message)
    {
        try
        {
            lock (Gate)
                File.AppendAllText(_path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {level} {message}\n");
        }
        catch (Exception) { /* logging must never take the game down */ }
        try
        {
            if (level == "ERROR") MegaCrit.Sts2.Core.Logging.Log.Error("[Sts2Advisor] " + message);
            else MegaCrit.Sts2.Core.Logging.Log.Info("[Sts2Advisor] " + message);
        }
        catch (Exception) { }
    }
}
