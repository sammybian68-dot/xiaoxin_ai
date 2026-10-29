package com.xiaoxin.util;

import java.util.ArrayList;
import java.util.List;

/**
 * 唤醒词和退出词同一套比对规则（服务端 server/talk.py 的 strip_wake_word / is_exit 是同一套，改一边要改另一边）：
 *   - 去掉标点空格、英文转小写再比
 *   - 「小新」认成同音的「小心 / 小欣 / 小鑫」也算（端侧小模型实测常把小新认成小心）
 *   - 列表里命中任意一个就算（或的关系）
 * 区别只有一个：唤醒词看开头（后面可以接问题，one-shot），退出词要整句正好是它（「你先退下吧」不算）。
 */
public final class Keywords {
    private static final String PUNCT = "，。！？、；：,.!?;: 　";
    private static final String[] ALIASES = {"小心", "小欣", "小鑫"};
    private static final String[] LEADS = {"", "嘿", "哎", "喂", "嗨"};

    private Keywords() {}

    static String bare(String text) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (PUNCT.indexOf(c) < 0) {
                b.append(Character.toLowerCase(c));
            }
        }
        return b.toString();
    }

    /** 一个词能接受的写法：原样，加上「小新」换成同音字的。 */
    static List<String> forms(String word) {
        List<String> out = new ArrayList<>();
        String w = bare(word);
        out.add(w);
        if (w.contains("小新")) {
            for (String a : ALIASES) {
                out.add(w.replace("小新", a));
            }
        }
        return out;
    }

    /** 唤醒：去标点后以（语气词 +）某个唤醒词开头。 */
    public static boolean startsWithAny(String text, String[] words) {
        String b = bare(text);
        for (String word : words) {
            for (String f : forms(word)) {
                for (String lead : LEADS) {
                    if (!f.isEmpty() && b.startsWith(lead + f)) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    /** 退出：去标点后整句正好是某个退出词。 */
    public static boolean equalsAny(String text, String[] words) {
        String b = bare(text);
        if (b.isEmpty()) {
            return false;
        }
        for (String word : words) {
            for (String f : forms(word)) {
                if (b.equals(f)) {
                    return true;
                }
            }
        }
        return false;
    }

    /** 设置里存的是列表，网页传过来是逗号连起来的串。 */
    public static String[] split(String joined) {
        List<String> out = new ArrayList<>();
        for (String s : joined.split("[,，、;；\\s]+")) {
            if (!s.isEmpty() && !out.contains(s)) {
                out.add(s);
            }
        }
        return out.toArray(new String[0]);
    }
}
